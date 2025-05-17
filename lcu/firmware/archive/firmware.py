import time
import json
import threading
import struct
import csv
from enum import Enum
from queue import Queue

import paho.mqtt.client as mqtt
import pigpio
from pymodbus.client import ModbusSerialClient

# ----------------------------------------------------
# HighSpeedLogger Class
# ----------------------------------------------------
class HighSpeedLogger:
    def __init__(self, filename="lcu_highspeed_log.csv"):
        self.filename = filename
        self.file = open(self.filename, mode='w', newline='', buffering=1)
        self.csv_writer = csv.writer(self.file)
        self.csv_writer.writerow([
            "timestamp", "pos_ticks", "pos_mm", "pos_inches",
            "current", "load", "hold", "stable", "current_speed"
        ])
        self.queue = Queue(maxsize=100000)
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def log(self, data: dict):
        if not self.running:
            return
        try:
            self.queue.put_nowait(data)
        except:
            pass

    def _run(self):
        while self.running:
            record = self.queue.get()
            if record is None:
                break
            row = [
                record.get("timestamp", time.monotonic()),
                record.get("pos_ticks", 0),
                record.get("pos_mm", 0.0),
                record.get("pos_inches", 0.0),
                record.get("current", 0.0),
                record.get("load", 0.0),
                1 if record.get("hold", False) else 0,
                1 if record.get("stable", False) else 0,
                record.get("current_speed", 0.0)
            ]
            self.csv_writer.writerow(row)

    def stop(self):
        self.running = False
        self.queue.put(None)
        self.thread.join()
        self.file.close()

# ----------------------------------------------------
# LoadCell Class
# ----------------------------------------------------
class LoadCell:
    def __init__(self, port, baudrate, parity, stopbits, bytesize, timeout, slave_id):
        self.client = ModbusSerialClient(
            port=port,
            baudrate=baudrate,
            parity=parity,
            stopbits=stopbits,
            bytesize=bytesize,
            timeout=timeout
        )
        self.slave_id = slave_id
        self.connected = self.client.connect()

    def __del__(self):
        self.client.close()

    def decode_i32(self, registers):
        raw = (registers[0] << 16) | registers[1]
        return struct.unpack('>i', struct.pack('>I', raw))[0]

    def read_load_value(self):
        result = self.client.read_input_registers(address=4, count=2, slave=self.slave_id)
        if not result.isError():
            return self.decode_i32(result.registers)
        else:
            print("❌ Failed to read load value")
            return None

    def read_status_flags(self):
        result = self.client.read_discrete_inputs(address=0, count=2, slave=self.slave_id)
        if not result.isError():
            return result.bits[0], result.bits[1]  # hold, stable
        else:
            print("❌ Failed to read status flags")
            return None, None

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
        filtered_d = self.derivative_filter * derivative + (1 - self.derivative_filter) * self.prev_derivative
        output = self.kp * error + self.ki * self.integral + self.kd * filtered_d
        self.prev_error = error
        self.prev_derivative = filtered_d
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
    RUN_DURATION = 1
    RUN_CONTINUOUS = 2
    PID_POSITION = 3
    PID_CURRENT = 4
    PID_LOAD = 5
    PID_SPEED = 6
    HOMING = 7

class Direction(Enum):
    IDLE = 0
    FW = 1
    BW = 2


# ----------------------------------------------------
# MotorSystem Class (Main)
# ----------------------------------------------------
BROKER_IP = "192.168.2.1"
DEVICE_ID = "lcu"
MAX_CURRENT = 5.0
MOTOR_PINS = {"RPWM": 12, "LPWM": 13, "REN": 23, "LEN": 24}
ENC_A, ENC_B = 5, 6
PULSES_PER_MM = 72.0
ERROR_TOLERANCE = 0.002
HOMING_SPEED = 50
HOMING_TIMEOUT = 10.0
PID_UPDATE_INTERVAL = 0.001
LOAD_THREAD_SLEEP = 0.001
MAX_HOMING_RETRIES = 3

class MotorSystem:
    def __init__(self):
        self.client = mqtt.Client()
        self.client.connect(BROKER_IP, 1883, 60)
        self.client.loop_start()
        self.client.subscribe(f"{DEVICE_ID}/cmd")
        self.client.on_message = self.on_message

        self.mode = Mode.IDLE
        self.direction = Direction.IDLE
        self.target = 0
        self.pid_setpoint = 0.0
        self.encoder_pos = 0
        self.last_encoder_pos = 0
        self.current_speed = 0.0
        self.is_homed = False
        self.homing_in_progress = False
        self.last_speed_time = time.monotonic()
        self.last_pid_update = 0.0
        self.pid_active = False

        self.speed_pid = PIDController(2.0, 0.05, 0.2, 50.0, 0.2)
        self.state_lock = threading.Lock()
        self.pi = pigpio.pi()

        for pin in MOTOR_PINS.values():
            self.pi.set_mode(pin, pigpio.OUTPUT)
            self.pi.write(pin, 0)
        self.pi.write(MOTOR_PINS["REN"], 1)
        self.pi.write(MOTOR_PINS["LEN"], 1)

        self.pi.set_mode(ENC_A, pigpio.INPUT)
        self.pi.set_mode(ENC_B, pigpio.INPUT)
        self.pi.set_pull_up_down(ENC_A, pigpio.PUD_UP)
        self.pi.set_pull_up_down(ENC_B, pigpio.PUD_UP)
        self.pi.callback(ENC_A, pigpio.EITHER_EDGE, self._encoder_callback)
        self.pi.callback(ENC_B, pigpio.EITHER_EDGE, self._encoder_callback)

        self.load_cell = LoadCell(port='/dev/ttyUSB0', baudrate=9600, parity='E', stopbits=1, bytesize=8, timeout=1, slave_id=1)
        # self.logger = HighSpeedLogger()

        # Initialize IDs as integers with default value 0
        self.project_id = 0
        self.experiment_id = 0
        self.run_id = 0

        self.running = True
        threading.Thread(target=self.run_loop, daemon=True).start()
        threading.Thread(target=self.send_data_loop, daemon=True).start()

    def _encoder_callback(self, gpio, level, tick):
        A = self.pi.read(ENC_A)
        B = self.pi.read(ENC_B)
        if gpio == ENC_A:
            delta = -1 if A == B else 1
        else:
            delta = -1 if A != B else 1
        self.encoder_pos += delta

    def control_motor(self, duty_percent, direction):
        duty = int(1_000_000 * duty_percent / 100.0)
        self.pi.write(MOTOR_PINS["REN"], 1)
        self.pi.write(MOTOR_PINS["LEN"], 1)
        if direction == Direction.FW:
            self.pi.hardware_PWM(MOTOR_PINS["RPWM"], 0, 0)
            self.pi.hardware_PWM(MOTOR_PINS["LPWM"], 20000, duty)
        elif direction == Direction.BW:
            self.pi.hardware_PWM(MOTOR_PINS["LPWM"], 0, 0)
            self.pi.hardware_PWM(MOTOR_PINS["RPWM"], 20000, duty)
        else:
            self.pi.hardware_PWM(MOTOR_PINS["RPWM"], 0, 0)
            self.pi.hardware_PWM(MOTOR_PINS["LPWM"], 0, 0)

    def on_message(self, client, userdata, msg):
        try:
            print(f"Received: {msg.payload.decode()}")
            data = json.loads(msg.payload.decode())
            self.mode = Mode(data.get("mode", 0))
            self.direction = Direction(data.get("direction", 0))
            self.target = data.get("target", 50)
            self.duration = data.get("duration", 0)
            self.pid_setpoint = data.get("pid_setpoint", 0)
            self.project_id = data.get("project_id", 0)
            self.experiment_id = data.get("experiment_id", 0)
            self.run_id = data.get("run_id", 0)
            print(f"Received: Mode={self.mode.name}, Dir={self.direction.name}, Speed={self.target}, Setpoint={self.pid_setpoint}")
        except Exception as e:
            self.send_error(f"MQTT command error: {e}")


    def run_loop(self):
        while self.running:
            now = time.monotonic()
            dt = now - self.last_speed_time
            if dt > 0:
                delta = (self.encoder_pos - self.last_encoder_pos)
                self.current_speed = (delta / PULSES_PER_MM) / dt
                self.last_encoder_pos = self.encoder_pos
                self.last_speed_time = now

            with self.state_lock:
                mode = self.mode
                dir = self.direction
                targ = self.target

            # Store current mode and parameters if we need to return to them after homing
            if not self.is_homed and mode != Mode.HOMING and mode != Mode.IDLE:
                stored_mode = mode
                stored_dir = dir
                stored_targ = targ
                self.mode = Mode.HOMING
                mode = Mode.HOMING

            if mode == Mode.HOMING:
                self._do_homing()
                # If homing was successful and we have a stored mode, return to it
                if self.is_homed and 'stored_mode' in locals():
                    self.mode = stored_mode
                    self.direction = stored_dir
                    self.target = stored_targ
                    mode = stored_mode
                    print(f"Returning to mode: {mode.name}")

            elif mode == Mode.PID_SPEED:
                if now - self.last_pid_update >= PID_UPDATE_INTERVAL:
                    ref = targ if dir == Direction.FW else -targ
                    out = self.speed_pid.compute(ref, self.current_speed)
                    duty = max(min(abs(out), 100), 0)
                    direction = Direction.FW if out >= 0 else Direction.BW
                    self.control_motor(duty, direction)
                    self.last_pid_update = now

            elif mode == Mode.RUN_CONTINUOUS:
                self.control_motor(targ, dir)

            elif mode == Mode.IDLE:
                self.control_motor(0, Direction.IDLE)

            time.sleep(0.001)

    def _do_homing(self):
        if self.homing_in_progress:
            return
        self.homing_in_progress = True
        print("=== Homing (retracting) ===")

        for attempt in range(MAX_HOMING_RETRIES):
            last_pos = self.encoder_pos
            still_counter = 0
            start = time.monotonic()
            self.control_motor(HOMING_SPEED, Direction.BW)
            while time.monotonic() - start < HOMING_TIMEOUT:
                time.sleep(0.005)
                delta = abs(self.encoder_pos - last_pos)
                if delta == 0:
                    still_counter += 1
                else:
                    still_counter = 0
                if still_counter >= 10:
                    break
                last_pos = self.encoder_pos
            self.control_motor(0, Direction.IDLE)
            time.sleep(0.3)
            if still_counter >= 10:
                self.encoder_pos = 0
                self.is_homed = True
                self.homing_in_progress = False
                print("Homing complete")
                return
            else:
                print(f"⚠️ Homing attempt {attempt+1} failed, retrying...")

        print("Homing failed after max retries")
        self.homing_in_progress = False

    def send_data_loop(self):
        while self.running:
            pos_ticks = self.encoder_pos
            pos_mm = pos_ticks / PULSES_PER_MM
            pos_in = pos_mm / 25.4
            
            #load_value = self.load_cell.read_load_value() if self.load_cell.connected else 0.0
            #hold, stable = self.load_cell.read_status_flags() if self.load_cell.connected else (False, False)
            
            data = {
                "timestamp": time.monotonic(),
                "mode": self.mode.value,
                "direction": self.direction.value,
                "target": self.target,
                "setpoint": self.pid_setpoint,
                "pos_ticks": pos_ticks,
                "pos_mm": round(pos_mm, 3),
                "pos_inches": round(pos_in, 3),
                "current": 0.0,
                #"load": load_value if load_value is not None else 0.0,
                #"hold": 1 if hold else 0,
                # "stable": 1 if stable else 0,
                "current_speed": round(self.current_speed, 3),
                "experiment_id": self.experiment_id,
                "run_id": self.run_id,
                "project_id": self.project_id,
            }
            # self.logger.log(data)
            self.client.publish(f"{DEVICE_ID}/data", json.dumps(data))
            print(f"Published data: {data}") 
            time.sleep(0.2)

    def stop(self):
        self.running = False
        self.control_motor(0, Direction.IDLE)
        # self.logger.stop()
        self.client.loop_stop()
        self.pi.stop()
        if hasattr(self, 'load_cell'):
            del self.load_cell

if __name__ == "__main__":
    system = MotorSystem()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        system.stop()