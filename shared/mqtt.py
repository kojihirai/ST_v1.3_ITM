import time
import json
import threading
from enum import Enum
import random
import logging

import paho.mqtt.client as mqtt

# ----------------------------------------------------
# Software-in-the-Loop (SIL) Config
# ----------------------------------------------------
BROKER_IP = "192.168.2.1"
DEVICE_ID = "lcu"
PULSES_PER_MM = 72.0
PID_UPDATE_INTERVAL = 0.001

# ----------------------------------------------------
# PIDController Class
# ----------------------------------------------------
class PIDController:
    def __init__(self, kp, ki, kd, integral_limit=100.0, derivative_filter=0.1):
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.integral_limit = integral_limit
        self.derivative_filter = derivative_filter
        self.reset()

    def compute(self, setpoint, measured):
        now = time.monotonic()
        dt = now - self.last_time
        self.last_time = now
        if dt <= 0:
            dt = 1e-6
        error = setpoint - measured
        self.integral = max(min(self.integral + error * dt, self.integral_limit), -self.integral_limit)
        derivative = (error - self.prev_error) / dt
        filtered = self.derivative_filter * derivative + (1 - self.derivative_filter) * self.prev_derivative
        output = self.kp * error + self.ki * self.integral + self.kd * filtered
        self.prev_error = error
        self.prev_derivative = filtered
        return output

    def reset(self):
        self.integral = 0.0
        self.prev_error = 0.0
        self.prev_derivative = 0.0
        self.last_time = time.monotonic()

# ----------------------------------------------------
# Enums
# ----------------------------------------------------
class Mode(Enum):
    IDLE = 0
    RUN = 1
    RESET = 2

class Direction(Enum):
    IDLE = 0
    FW = 1
    BW = 2

class MessageType(Enum):
    CMD = 0
    CONFIG = 1

# ----------------------------------------------------
# Simulated GPIO & Encoder & Motor & Load Cell
# interfaces matching hardware API
# ----------------------------------------------------
class SILGPIO:
    def __init__(self):
        self.pins = {}
    def set_mode(self, pin, mode): pass
    def write(self, pin, level): pass
    def hardware_PWM(self, pin, freq, duty):
        # freq, duty simulated
        pass

class EncoderSimulator:
    def __init__(self):
        self.ticks = 0
    def simulate_edge(self, delta):
        self.ticks += delta
    def read(self):
        return self.ticks

class LoadCellSimulator:
    def read_load_value(self):
        return random.uniform(0.0, 100.0)
    def read_status_flags(self):
        return random.choice([True, False]), random.choice([True, False])

# ----------------------------------------------------
# MQTTHandler Class
# ----------------------------------------------------
class MQTTHandler:
    def __init__(self, broker_ip, device_id):
        self.client = mqtt.Client()
        self.client.on_message = self._on_message
        self.device_id = device_id
        self.cmd_callback = None

    def register_callback(self, callback):
        self.cmd_callback = callback

    def start(self):
        self.client.connect(BROKER_IP, 1883, 60)
        self.client.subscribe(f"{self.device_id}/cmd")
        self.client.loop_start()
        logging.info("MQTTHandler started")

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload)
            if self.cmd_callback:
                self.cmd_callback(data)
        except Exception as e:
            logging.error(f"MQTT parse error: {e}")

    def publish(self, topic, payload):
        self.client.publish(topic, json.dumps(payload))

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()
        logging.info("MQTTHandler stopped")

# ----------------------------------------------------
# MotorSystem Class (SIL, same interface as hardware)
# ----------------------------------------------------
class MotorSystem:
    def __init__(self, mqtt_handler):
        # MQTT
        self.mqtt = mqtt_handler
        self.mqtt.register_callback(self.on_message)

        # Mode/state
        self.mode = Mode.IDLE
        self.direction = Direction.IDLE
        self.target = 0
        self.pid_setpoint = 0.0
        self.encoder_ticks = 0
        self.current_speed = 0.0
        self.is_homed = False
        self.last_speed_time = time.monotonic()
        self.last_pid_update = 0.0
        self.state_lock = threading.Lock()

        # SIL hardware
        self.gpio = SILGPIO()
        self.encoder = EncoderSimulator()
        self.load_cell = LoadCellSimulator()

        # PID
        self.speed_pid = PIDController(2.0, 0.05, 0.2, 50.0, 0.2)

        self.running = False

    def on_message(self, client_data):
        try:
            with self.state_lock:
                self.mode = Mode(client_data.get("mode", 0))
                self.direction = Direction(client_data.get("direction", 0))
                self.target = client_data.get("target", 0)
                self.pid_setpoint = client_data.get("pid_setpoint", 0)
            logging.info(f"MQTT cmd: Mode={self.mode.name}, Dir={self.direction.name}, Target={self.target}, Setpoint={self.pid_setpoint}")
        except Exception as e:
            logging.error(f"Command parse error: {e}")

    def control_motor(self, duty, direction):
        # Same signature as hardware version
        self.gpio.write('REN', 1)
        self.gpio.write('LEN', 1)
        if direction == Direction.FW:
            self.gpio.hardware_PWM('RPWM', 20000, int(duty * 10000))
        elif direction == Direction.BW:
            self.gpio.hardware_PWM('LPWM', 20000, int(duty * 10000))
        else:
            self.gpio.hardware_PWM('RPWM', 0, 0)
            self.gpio.hardware_PWM('LPWM', 0, 0)

    def run_loop(self):
        while self.running:
            now = time.monotonic()
            dt = now - self.last_speed_time
            if dt > 0:
                ticks = self.encoder.read() - self.encoder_ticks
                self.current_speed = (ticks / PULSES_PER_MM) / dt
                self.encoder_ticks = self.encoder.read()
                self.last_speed_time = now

            with self.state_lock:
                mode = self.mode
                direction = self.direction
                target = self.target

            if mode == Mode.HOMING:
                # simulate homing
                self.encoder_ticks = 0
                self.is_homed = True
                self.mode = Mode.IDLE

            if mode == Mode.PID_SPEED:
                if now - self.last_pid_update >= PID_UPDATE_INTERVAL:
                    ref = target if direction == Direction.FW else -target
                    out = self.speed_pid.compute(ref, self.current_speed)
                    duty = max(min(abs(out), 100), 0)
                    dir_out = Direction.FW if out >= 0 else Direction.BW
                    self.control_motor(duty, dir_out)
                    self.last_pid_update = now
            elif mode == Mode.RUN_CONTINUOUS:
                self.control_motor(target, direction)
            elif mode == Mode.IDLE:
                self.control_motor(0, Direction.IDLE)

            time.sleep(0.001)

    def send_data_loop(self):
        while self.running:
            pos_mm = self.encoder.read() / PULSES_PER_MM
            load = self.load_cell.read_load_value()
            data = {
                "timestamp": time.monotonic(),
                "mode": self.mode.value,
                "direction": self.direction.value,
                "target": self.target,
                "setpoint": self.pid_setpoint,
                "pos_ticks": self.encoder.read(),
                "pos_mm": round(pos_mm, 3),
                "load": round(load, 3),
                "current_speed": round(self.current_speed, 3)
            }
            self.mqtt.publish(f"{DEVICE_ID}/data", data)
            logging.info(f"Published data: {data}")
            time.sleep(0.2)

    def start(self):
        self.running = True
        self.mqtt.start()
        threading.Thread(target=self.run_loop, daemon=True).start()
        threading.Thread(target=self.send_data_loop, daemon=True).start()

    def stop(self):
        self.running = False
        self.control_motor(0, Direction.IDLE)
        self.mqtt.stop()

# ----------------------------------------------------
# Core Orchestrator
# ----------------------------------------------------
class Core:
    def __init__(self):
        self.mqtt = MQTTHandler(BROKER_IP, DEVICE_ID)
        self.system = MotorSystem(self.mqtt)

    def start(self):
        logging.basicConfig(level=logging.INFO)
        self.system.start()

    def stop(self):
        self.system.stop()

if __name__ == "__main__":
    core = Core()
    core.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        core.stop()