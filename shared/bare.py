import time
import json
import threading
from enum import Enum
import random
import logging
import os
import pigpio
import paho.mqtt.client as mqtt
from dataclasses import dataclass
from typing import Dict, Optional
from queue import Queue
import csv
from pymodbus.client import ModbusSerialClient
import struct
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# ----------------------------------------------------
# Enums
# ----------------------------------------------------
class Mode(Enum):
    IDLE = 0
    RUN = 1
    RESET = 2
    HOMING = 3
    PID_SPEED = 4

class Direction(Enum):
    IDLE = 0
    FW = 1
    BW = 2

class MessageType(Enum):
    CMD = 0
    CONFIG = 1

# ----------------------------------------------------
# Configuration Classes
# ----------------------------------------------------
@dataclass
class LoadCellConfig:
    port: str = os.getenv('LOAD_CELL_PORT', '/dev/ttyUSB0')
    baudrate: int = int(os.getenv('LOAD_CELL_BAUDRATE', '9600'))
    parity: str = os.getenv('LOAD_CELL_PARITY', 'E')
    stopbits: int = int(os.getenv('LOAD_CELL_STOPBITS', '1'))
    bytesize: int = int(os.getenv('LOAD_CELL_BYTESIZE', '8'))
    timeout: float = float(os.getenv('LOAD_CELL_TIMEOUT', '1.0'))
    slave_id: int = int(os.getenv('LOAD_CELL_SLAVE_ID', '1'))
    register_address: int = int(os.getenv('LOAD_CELL_REGISTER_ADDRESS', '0'))
    scale_factor: float = float(os.getenv('LOAD_CELL_SCALE_FACTOR', '1.0'))
    zero_offset: float = float(os.getenv('LOAD_CELL_ZERO_OFFSET', '0.0'))

@dataclass
class DataLoggerConfig:
    log_interval: float = float(os.getenv('LOG_INTERVAL', '0.0002'))
    buffer_size: int = int(os.getenv('LOG_BUFFER_SIZE', '10000'))
    log_file: str = None
    log_format: str = os.getenv('LOG_FORMAT', 'csv')
    fields: list = None

    def __post_init__(self):
        if self.fields is None:
            self.fields = [
                "timestamp",
                "pos_ticks",
                "pos_mm",
                "pos_inches",
                "load",
                "current_speed"
            ]
        if self.log_file is None:
            self.log_file = f"data_{int(time.time())}.{self.log_format}"

@dataclass
class MotorConfig:
    pwm_frequency: int = int(os.getenv('MOTOR_PWM_FREQUENCY', '20000'))
    pwm_range: int = 1000000  # 1M for 100% duty cycle
    enable_active_high: bool = os.getenv('MOTOR_ENABLE_ACTIVE_HIGH', 'true').lower() == 'true'
    pins: Dict[str, int] = None

    def __post_init__(self):
        if self.pins is None:
            self.pins = {
                "RPWM": int(os.getenv('MOTOR_RPWM_PIN', '12')),
                "LPWM": int(os.getenv('MOTOR_LPWM_PIN', '13')),
                "REN": int(os.getenv('MOTOR_REN_PIN', '23')),
                "LEN": int(os.getenv('MOTOR_LEN_PIN', '24'))
            }

@dataclass
class EncoderConfig:
    pulses_per_mm: float = float(os.getenv('PULSES_PER_MM', '72.0'))
    pins: Dict[str, int] = None
    pull_up: bool = os.getenv('ENCODER_PULL_UP', 'true').lower() == 'true'

    def __post_init__(self):
        if self.pins is None:
            self.pins = {
                "A": int(os.getenv('ENCODER_A_PIN', '5')),
                "B": int(os.getenv('ENCODER_B_PIN', '6'))
            }

@dataclass
class SystemConfig:
    device_name: str = os.getenv('DEVICE_NAME', 'lcu')
    broker_ip: str = os.getenv('BROKER_IP', '192.168.2.1')
    broker_port: int = int(os.getenv('BROKER_PORT', '1883'))
    pid_update_interval: float = float(os.getenv('PID_UPDATE_INTERVAL', '0.001'))
    homing_speed: float = float(os.getenv('HOMING_SPEED', '50'))
    homing_timeout: float = float(os.getenv('HOMING_TIMEOUT', '10.0'))
    max_homing_retries: int = int(os.getenv('MAX_HOMING_RETRIES', '3'))
    motor: MotorConfig = None
    encoder: EncoderConfig = None
    load_cell: LoadCellConfig = None
    data_logger: DataLoggerConfig = None
    pid_gains: Dict[str, float] = None

    def __post_init__(self):
        if self.motor is None:
            self.motor = MotorConfig()
        if self.encoder is None:
            self.encoder = EncoderConfig()
        if self.load_cell is None:
            self.load_cell = LoadCellConfig()
        if self.data_logger is None:
            self.data_logger = DataLoggerConfig()
        if self.pid_gains is None:
            self.pid_gains = {
                "kp": float(os.getenv('PID_KP', '2.0')),
                "ki": float(os.getenv('PID_KI', '0.05')),
                "kd": float(os.getenv('PID_KD', '0.2')),
                "integral_limit": float(os.getenv('PID_INTEGRAL_LIMIT', '50.0')),
                "derivative_filter": float(os.getenv('PID_DERIVATIVE_FILTER', '0.2'))
            }

# ----------------------------------------------------
# MQTTHandler Class
# ----------------------------------------------------
class MQTTHandler:
    def __init__(self, config: SystemConfig):
        self.client = mqtt.Client()
        self.client.on_message = self._on_message
        self.device_id = config.device_name
        self.broker_ip = config.broker_ip
        self.broker_port = config.broker_port
        self.cmd_callback = None

    def register_callback(self, callback):
        self.cmd_callback = callback

    def start(self):
        self.client.connect(self.broker_ip, self.broker_port, 60)
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
# Local HS Log 
# ----------------------------------------------------
class LocalHSLog:
    def __init__(self):
        self.log_file = f"{DEVICE_NAME}.log"
        self.log_file = open(self.log_file, "w")
        self.log_file.write(f"Log started at {time.time()}\n")

    def write(self, message):
        self.log_file.write(f"{time.time()}: {message}\n")
        self.log_file.flush()

    def close(self):
        self.log_file.close()

# ----------------------------------------------------
# High Speed Logger
# ----------------------------------------------------
class HighSpeedLogger:
    def __init__(self, config: DataLoggerConfig):
        self.config = config
        self.queue = Queue(maxsize=config.buffer_size)
        self.running = False
        self.file = None
        self.writer = None
        self.thread = None

    def start(self):
        if self.config.log_format == 'csv':
            self.file = open(self.config.log_file, 'w', newline='')
            self.writer = csv.DictWriter(self.file, fieldnames=self.config.fields)
            self.writer.writeheader()
        else:  # binary
            self.file = open(self.config.log_file, 'wb')
            # Write header with field names
            header = struct.pack('!I', len(self.config.fields))
            for field in self.config.fields:
                header += struct.pack('!I', len(field)) + field.encode()
            self.file.write(header)

        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def log(self, data: dict):
        if not self.running:
            return
        try:
            self.queue.put_nowait(data)
        except:
            pass  # Queue is full, drop the data point

    def _run(self):
        while self.running:
            try:
                data = self.queue.get(timeout=1.0)
                if self.config.log_format == 'csv':
                    self.writer.writerow(data)
                else:  # binary
                    # Pack data in order of fields
                    values = [data[field] for field in self.config.fields]
                    self.file.write(struct.pack('!d' * len(values), *values))
                self.file.flush()
            except:
                continue

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join()
        if self.file:
            self.file.close()

# ----------------------------------------------------
# Load Cell Interface
# ----------------------------------------------------
class LoadCell:
    def __init__(self, config: LoadCellConfig):
        self.config = config
        self.client = ModbusSerialClient(
            port=config.port,
            baudrate=config.baudrate,
            parity=config.parity,
            stopbits=config.stopbits,
            bytesize=config.bytesize,
            timeout=config.timeout
        )
        self.connected = False
        self.connect()

    def connect(self):
        try:
            self.connected = self.client.connect()
            if self.connected:
                logging.info("Load cell connected successfully")
            else:
                logging.error("Failed to connect to load cell")
        except Exception as e:
            logging.error(f"Load cell connection error: {e}")
            self.connected = False

    def read_load_value(self) -> float:
        if not self.connected:
            return 0.0
        try:
            result = self.client.read_holding_registers(
                self.config.register_address,
                2,
                unit=self.config.slave_id
            )
            if result.isError():
                return 0.0
            # Combine two registers into a 32-bit integer
            raw_value = (result.registers[0] << 16) | result.registers[1]
            # Convert to float and apply scaling
            return (raw_value * self.config.scale_factor) + self.config.zero_offset
        except Exception as e:
            logging.error(f"Load cell read error: {e}")
            return 0.0

    def close(self):
        if self.connected:
            self.client.close()
            self.connected = False

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
# MotorSystem Class
# ----------------------------------------------------
class MotorSystem:
    def __init__(self, config: SystemConfig):
        self.config = config
        
        # MQTT
        self.mqtt = MQTTHandler(config)
        self.mqtt.register_callback(self.on_message)

        self.mode = Mode.IDLE
        self.direction = Direction.IDLE
        self.setpoint = 0
        self.target = 0
        self.pid_setpoint = 0.0

        self.last_speed_time = time.monotonic()
        self.last_pid_update = 0.0
        self.state_lock = threading.Lock()

        # Hardware initialization
        self.pi = pigpio.pi()
        if not self.pi.connected:
            raise RuntimeError("Failed to connect to pigpio daemon")

        # Initialize motor pins
        for pin in self.config.motor.pins.values():
            self.pi.set_mode(pin, pigpio.OUTPUT)
            self.pi.write(pin, 0)
        
        # Set enable pins
        enable_value = 1 if self.config.motor.enable_active_high else 0
        self.pi.write(self.config.motor.pins["REN"], enable_value)
        self.pi.write(self.config.motor.pins["LEN"], enable_value)

        # Initialize encoder pins
        for pin in self.config.encoder.pins.values():
            self.pi.set_mode(pin, pigpio.INPUT)
            if self.config.encoder.pull_up:
                self.pi.set_pull_up_down(pin, pigpio.PUD_UP)
            else:
                self.pi.set_pull_up_down(pin, pigpio.PUD_DOWN)
        
        self.pi.callback(self.config.encoder.pins["A"], pigpio.EITHER_EDGE, self._encoder_callback)
        self.pi.callback(self.config.encoder.pins["B"], pigpio.EITHER_EDGE, self._encoder_callback)

        # Initialize load cell
        self.load_cell = LoadCell(self.config.load_cell)

        # Initialize data logger
        self.logger = HighSpeedLogger(self.config.data_logger)

        self.encoder_pos = 0
        self.last_encoder_pos = 0
        self.current_speed = 0.0
        self.is_homed = False
        self.homing_in_progress = False

        # PID
        self.speed_pid = PIDController(
            self.config.pid_gains["kp"],
            self.config.pid_gains["ki"],
            self.config.pid_gains["kd"],
            self.config.pid_gains["integral_limit"],
            self.config.pid_gains["derivative_filter"]
        )

        # Initialize IDs
        self.project_id = 0
        self.experiment_id = 0
        self.run_id = 0

        self.running = False

    def on_message(self, client_data):
        try:
            with self.state_lock:
                # Update mode and control parameters
                self.mode = Mode(client_data.get("mode", 0))
                self.direction = Direction(client_data.get("direction", 0))
                self.target = client_data.get("target", 0)
                self.pid_setpoint = client_data.get("pid_setpoint", 0)
                
                # Update experiment tracking IDs
                if "project_id" in client_data:
                    self.project_id = int(client_data["project_id"])
                if "experiment_id" in client_data:
                    self.experiment_id = int(client_data["experiment_id"])
                if "run_id" in client_data:
                    self.run_id = int(client_data["run_id"])
                
            logging.info(f"MQTT cmd: Mode={self.mode.name}, Dir={self.direction.name}, Target={self.target}, Setpoint={self.pid_setpoint}")
            logging.info(f"IDs: Project={self.project_id}, Experiment={self.experiment_id}, Run={self.run_id}")
        except Exception as e:
            logging.error(f"Command parse error: {e}")

    def _encoder_callback(self, gpio, level, tick):
        A = self.pi.read(self.config.encoder.pins["A"])
        B = self.pi.read(self.config.encoder.pins["B"])
        if gpio == self.config.encoder.pins["A"]:
            delta = -1 if A == B else 1
        else:
            delta = -1 if A != B else 1
        self.encoder_pos += delta

    def control_motor(self, duty, direction):
        duty = int(1_000_000 * duty / 100.0)  # Convert percentage to PWM range
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

    def _do_homing(self):
        if self.homing_in_progress:
            return
        self.homing_in_progress = True
        logging.info("=== Homing (retracting) ===")

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
                logging.info("Homing complete")
                return
            else:
                logging.warning(f"⚠️ Homing attempt {attempt+1} failed, retrying...")

        logging.error("Homing failed after max retries")
        self.homing_in_progress = False

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
                direction = self.direction
                target = self.target

            # Store current mode and parameters if we need to return to them after homing
            if not self.is_homed and mode != Mode.HOMING and mode != Mode.IDLE:
                stored_mode = mode
                stored_dir = direction
                stored_targ = target
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
                    logging.info(f"Returning to mode: {mode.name}")

            elif mode == Mode.PID_SPEED:
                if now - self.last_pid_update >= PID_UPDATE_INTERVAL:
                    ref = target if direction == Direction.FW else -target
                    out = self.speed_pid.compute(ref, self.current_speed)
                    duty = max(min(abs(out), 100), 0)
                    dir_out = Direction.FW if out >= 0 else Direction.BW
                    self.control_motor(duty, dir_out)
                    self.last_pid_update = now

            elif mode == Mode.RUN:
                self.control_motor(target, direction)

            elif mode == Mode.IDLE:
                self.control_motor(0, Direction.IDLE)

            time.sleep(0.001)

    def send_data_loop(self):
        while self.running:
            pos_ticks = self.encoder_pos
            pos_mm = pos_ticks / PULSES_PER_MM
            pos_in = pos_mm / 25.4
            
            data = {
                "timestamp": time.monotonic(),
                "mode": self.mode.value,
                "direction": self.direction.value,
                "target": self.target,
                "setpoint": self.pid_setpoint,
                "pos_ticks": pos_ticks,
                "pos_mm": round(pos_mm, 3),
                "pos_inches": round(pos_in, 3),
                "current_speed": round(self.current_speed, 3),
                "experiment_id": self.experiment_id,
                "run_id": self.run_id,
                "project_id": self.project_id,
            }
            self.mqtt.publish(f"{DEVICE_NAME}/data", data)
            logging.info(f"Published data: {data}")
            time.sleep(0.2)

    def start(self):
        self.running = True
        self.mqtt.start()
        self.logger.start()
        threading.Thread(target=self.run_loop, daemon=True).start()
        threading.Thread(target=self.send_data_loop, daemon=True).start()

    def stop(self):
        self.running = False
        self.control_motor(0, Direction.IDLE)
        self.mqtt.stop()
        self.logger.stop()
        self.load_cell.close()
        self.pi.stop()

# ----------------------------------------------------
# Core Orchestrator
# ----------------------------------------------------
class Core:
    def __init__(self):
        self.config = SystemConfig()
        self.mqtt = MQTTHandler(self.config)
        self.system = MotorSystem(self.config)

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