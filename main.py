import machine
from machine import ADC
from machine import Pin, UART
from time import sleep_ms
from ucryptolib import aes
import ustruct as struct
import ubinascii
import ubluetooth
import json
import _thread
import sys
import gc
from time import ticks_ms, ticks_diff

DEBUG = False
LOG_ENABLED = False
REAL_HARDWARE = True
LOW_BATTERY_THRESHOLD_V = 3.3
LOW_BATTERY_CRITICAL_V = 3.15
LOW_BATTERY_SHUTDOWN_V = 3.0
BUTTON_HOLD_MS = 3000
BUTTON_CLICK_DEBOUNCE_MS = 50
BUTTON_IRQ_QUEUE_SIZE = 8
MAIN_LOOP_SLEEP_MS = 100
BT_BROADCAST_INTERVAL = 300
LOG_MAX_ENTRIES = 100
LOG_FILENAME = "lora_log.txt"
LOW_BATTERY_WARNING_INTERVAL_MS = 10000
LOW_BATTERY_CRITICAL_INTERVAL_MS = 5000
BUTTON_DOUBLE_CLICK_MS = 700
BLUE_BLINK_MS = 300
GREEN_RX_PULSE_MS = 80
GREEN_FORWARD_BLINK_MS = 80
RED_ERROR_PULSE_MS = 120
RED_WARNING_BLINK_MS = 300
VERBOSE_MODE = True
BLE_CONNECTED = False
BUTTON_IRQ_TIMES = [0] * BUTTON_IRQ_QUEUE_SIZE
BUTTON_IRQ_STATES = [1] * BUTTON_IRQ_QUEUE_SIZE
BUTTON_IRQ_HEAD = 0
BUTTON_IRQ_TAIL = 0
BUTTON_IRQ_LAST_AT = 0

def debug_print(*args, **kwargs):
    if DEBUG:
        print(*args, **kwargs)


def button_irq(pin):
    global BUTTON_IRQ_HEAD, BUTTON_IRQ_TAIL, BUTTON_IRQ_LAST_AT
    now = ticks_ms()
    if BUTTON_IRQ_LAST_AT and ticks_diff(now, BUTTON_IRQ_LAST_AT) < 20:
        return

    next_head = (BUTTON_IRQ_HEAD + 1) % BUTTON_IRQ_QUEUE_SIZE
    if next_head == BUTTON_IRQ_TAIL:
        return

    BUTTON_IRQ_TIMES[BUTTON_IRQ_HEAD] = now
    BUTTON_IRQ_STATES[BUTTON_IRQ_HEAD] = 0 if pin.value() == 0 else 1
    BUTTON_IRQ_HEAD = next_head
    BUTTON_IRQ_LAST_AT = now


def set_verbose_mode(enabled):
    global VERBOSE_MODE
    VERBOSE_MODE = enabled
    if LEDS is not None:
        if BLE_CONNECTED:
            if VERBOSE_MODE:
                LEDS.blue_connected()
            else:
                LEDS.base_state['blue'] = 1
                LEDS._set_pin('blue', 1)
    show_verbose_mode_status()


def toggle_verbose_mode():
    set_verbose_mode(not VERBOSE_MODE)


def show_verbose_mode_status():
    if VERBOSE_MODE:
        if LEDS is not None:
            LEDS.green_pulse()
    else:
        if LEDS is not None:
            LEDS.green_double_blink()
            debug_print("Silent mode: green status double pulse")


class LED_MANAGER():
    def __init__(self, blue_pin, red_pin, green_pin):
        self.pins = {
            'blue': blue_pin,
            'red': red_pin,
            'green': green_pin,
        }
        self.base_state = {
            'blue': 1,
            'red': 1,
            'green': 1,
        }
        self.effects = {}

    def _set_pin(self, name, value):
        self.pins[name].value(value)

    def _start_blink(self, name, on_ms, off_ms, count=None):
        self.effects[name] = {
            'phase': 'on',
            'next_at': ticks_ms() + on_ms,
            'on_ms': on_ms,
            'off_ms': off_ms,
            'count': count,
            'mask_colors': set(),
        }

    def _start_masked_blink(self, name, on_ms, off_ms, count=None, mask_colors=None):
        self._start_blink(name, on_ms, off_ms, count=count)
        if mask_colors:
            self.effects[name]['mask_colors'] = set(mask_colors)

    def _cancel(self, name):
        if name in self.effects:
            del self.effects[name]

    def update(self):
        now = ticks_ms()

        for name in list(self.effects):
            effect = self.effects.get(name)
            if effect is None:
                continue

            if ticks_diff(now, effect['next_at']) < 0:
                continue

            if effect['phase'] == 'on':
                effect['phase'] = 'off'
                effect['next_at'] = now + effect['off_ms']
            else:
                if effect['count'] is not None:
                    effect['count'] -= 1
                    if effect['count'] <= 0:
                        del self.effects[name]
                        continue
                effect['phase'] = 'on'
                effect['next_at'] = now + effect['on_ms']

        outputs = dict(self.base_state)
        masked_colors = set()
        for name, effect in self.effects.items():
            if effect['phase'] == 'on':
                outputs[name] = 0
                masked_colors.update(effect['mask_colors'])
            else:
                outputs[name] = 1

        for color in masked_colors:
            outputs[color] = 1

        for name, value in outputs.items():
            self._set_pin(name, value)

    def off_all(self):
        self.effects.clear()
        self.base_state = {
            'blue': 1,
            'red': 1,
            'green': 1,
        }
        for pin in self.pins.values():
            pin.value(1)

    def blue_connected(self):
        self._cancel('blue')
        self.base_state['blue'] = 0 if VERBOSE_MODE else 1
        self._set_pin('blue', self.base_state['blue'])

    def blue_waiting(self):
        self.base_state['blue'] = 1
        self._start_blink('blue', BLUE_BLINK_MS, BLUE_BLINK_MS)

    def green_pulse(self):
        self._start_masked_blink('green', GREEN_RX_PULSE_MS, 0, count=1, mask_colors=['blue', 'red'])

    def green_double_blink(self):
        self._start_masked_blink('green', GREEN_FORWARD_BLINK_MS, GREEN_FORWARD_BLINK_MS, count=2, mask_colors=['blue', 'red'])

    def red_error_pulse(self):
        self._start_masked_blink('red', RED_ERROR_PULSE_MS, 0, count=1, mask_colors=['blue', 'green'])

    def red_pulse(self):
        self._start_masked_blink('red', RED_WARNING_BLINK_MS, 0, count=1, mask_colors=['blue', 'green'])

    def red_solid(self):
        self._cancel('red')
        self.base_state['red'] = 0
        self._set_pin('red', 0)

    def green_solid(self):
        self._cancel('green')
        self.base_state['green'] = 0
        self._set_pin('green', 0)


LEDS = None

if REAL_HARDWARE:
    VBAT_IN = ADC(Pin(39))
    BUTTON = Pin(35, Pin.IN)
    POWER_CTRL = Pin(12, Pin.OUT)
    LED_BLUE = Pin(21, Pin.OUT)
    LED_RED = Pin(18, Pin.OUT)
    LED_GREEN = Pin(19, Pin.OUT)
    LORA_UART = UART(2, 9600, timeout=100, txbuf=1024, rxbuf=1024)
else:
    # Loko debug board pinout
    VBAT_IN = ADC(Pin(1))
    BUTTON = Pin(0, Pin.IN)
    POWER_CTRL = Pin(12, Pin.OUT)
    LED_BLUE = Pin(47, Pin.OUT)
    LED_RED = Pin(36, Pin.OUT)
    LED_GREEN = Pin(37, Pin.OUT)
    LORA_UART = UART(2, 9600)

use_command_line_parser = True  # Set to True to enable command line interface

class SETTINGS():

    data = {
        'id2': 0,
        'freq': 868000000,
        'p2p_key': "00" * 32,
    }

    def __init__(self, file_name='settings.json'):
        self.file_name = file_name
        self.load()

    def save(self):
        debug_print('Save settings to {}:{}'.format(self.file_name, self.data))
        with open(self.file_name, "w") as fp:
            json.dump(self.data, fp)

    def load(self):
        try:
            with open(self.file_name, "r") as fp:
                self.data = json.load(fp)
                if self.data['freq'] < 1000:
                    self.data['freq'] = self.data['freq'] * 1000000
                    self.save()
        except Exception as inst:
            debug_print(inst)
            debug_print("Settings file not found create new and use default settings")
            self.save()
        debug_print('Load settings from {}:{}'.format(self.file_name, self.data))


class LOG_MANAGER():
    def __init__(self, max_entries=50, filename="lora_log.txt", enabled=True):
        self.log = []
        self.max_entries = max_entries
        self.filename = filename
        self.file_mode = "a"  # append mode by default
        self.enabled = enabled
        self.lock = _thread.allocate_lock()

        if not self.enabled:
            return

        try:
            self._load_from_file()
        except:
            debug_print("No existing log file found or couldn't read it")

    def _load_from_file(self):
        if not self.enabled:
            return
        try:
            with open(self.filename, "r") as f:
                lines = f.readlines()

            if len(lines) > self.max_entries:
                lines = lines[-self.max_entries:]

            for line in lines:
                if line.strip():
                    try:
                        timestamp = line[1:line.find("]")]
                        data = line[line.find("]") + 2:].strip()
                        self.log.append({'timestamp': timestamp, 'data': data})
                    except:
                        debug_print(f"Couldn't parse log line: {line}")
        except:
            self.log = []

    def add_entry(self, data):
        if not self.enabled:
            return
        self.lock.acquire()
        try:
            try:
                timestamp = machine.RTC().datetime()
                time_str = "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
                    timestamp[0], timestamp[1], timestamp[2],
                    timestamp[4], timestamp[5], timestamp[6]
                )
            except:
                time_str = "LOG-" + str(len(self.log) + 1)

            log_entry = {'timestamp': time_str, 'data': data}

            if len(self.log) >= self.max_entries:
                self.log.pop(0)

            self.log.append(log_entry)

            try:
                with open(self.filename, self.file_mode) as f:
                    f.write(f"[{time_str}] {data}\n")
                self.file_mode = "a"
            except:
                debug_print("Failed to write log to file")
        finally:
            self.lock.release()

    def get_all_logs(self):
        if not self.enabled:
            return []
        self.lock.acquire()
        try:
            return list(self.log)
        finally:
            self.lock.release()

    def clear_logs(self):
        if not self.enabled:
            self.log = []
            return
        self.lock.acquire()
        try:
            self.log = []
            try:
                with open(self.filename, "w") as f:
                    f.write("")
                debug_print("Log file cleared")
            except:
                debug_print("Failed to clear log file")
        finally:
            self.lock.release()

    def export_logs(self):
        if not self.enabled:
            return ""
        self.lock.acquire()
        try:
            output = ""
            for entry in self.log:
                output += f"[{entry['timestamp']}] {entry['data']}\n"
            return output
        finally:
            self.lock.release()


class COMMAND_RECEIVER():

    def __init__(self, settings_obj, log_manager):
        self.exit_request = False
        self.settings = settings_obj
        self.log_manager = log_manager
        self.commands = {
            'set': {'handler': self.set_handler, 'info': '\'set gid2 VALUE\' or \'set gfreq VALUE\' or \'set gp2p_key VALUE\''},
            'info': {'handler': self.get_info, 'info': 'print current settings'},
            'help': {'handler': self.print_help, 'info': 'show this text'},
            'mem': {'handler': self.show_mem, 'info': 'show memory usage statistics'},
            'exit': {'handler': self.exit_app, 'info': 'exit application'},
        }
        if self.log_manager.enabled:
            self.commands.update({
                'log': {'handler': self.show_log, 'info': 'show log entries, optional: \'log NUMBER\' to show last N entries'},
                'clearlog': {'handler': self.clear_log, 'info': 'clear all log entries'},
                'savelog': {'handler': self.save_log, 'info': 'force save log entries to flash'},
            })
        _thread.start_new_thread(self.receiver_thread, ())

    def set_handler(self, tag, number):
        if tag == 'gid2':
            try:
                param = int(number)
                self.settings.data['id2'] = param
                self.settings.save()
                print('OK')
            except (ValueError, TypeError):
                print('Error: Expected numeric argument for gid2')

        elif tag == 'gfreq':
            try:
                param = int(number)
                if param < 100000000 or param > 1000000000:
                    print('Error: Invalid frequency, expected value in Hz between 100MHz and 1000MHz')
                    return
                self.settings.data['freq'] = param
                self.settings.save()
                print('OK')
            except (ValueError, TypeError):
                print('Error: Expected numeric argument for gfreq')

        elif tag == 'gp2p_key':
            # Validate that the key is a valid hex string of correct length
            if len(number) != 64 or not all(c in '0123456789abcdefABCDEF' for c in number):
                print('Error: Expected 64 character hexadecimal string for p2p_key')
                return
            self.settings.data['p2p_key'] = number
            self.settings.save()
            print('OK')
        else:
            print('Error: Unknown parameter')

    def get_info(self, *args):
        print('Settings:')
        print(f'  Device ID (id2): {self.settings.data["id2"]}')
        print(f'  Frequency: {self.settings.data["freq"]} Hz')
        print(f'  P2P Key: {self.settings.data["p2p_key"]}')
        print('OK')

    def print_help(self, *args):
        print('Available commands:')
        for cmd in self.commands:
            print('{} - {}'.format(cmd, self.commands[cmd]['info']))
        print('OK')

    def show_log(self, num_entries=None):
        logs = self.log_manager.get_all_logs()
        if not logs:
            print('Log is empty')
            print('OK')
            return

        if num_entries is not None:
            try:
                num = int(num_entries)
                logs = logs[-num:] if num < len(logs) else logs
            except ValueError:
                print('Error: Expected numeric argument for number of entries')
                return

        print('--- Log Entries ---')
        for i, entry in enumerate(logs):
            print(f'[{entry["timestamp"]}] {entry["data"]}')
        print('------------------')
        print('OK')

    def clear_log(self, *args):
        self.log_manager.clear_logs()
        print('Log cleared')
        print('OK')

    def show_mem(self, *args):
        gc.collect()
        free_mem = gc.mem_free()
        alloc_mem = gc.mem_alloc()
        total_mem = free_mem + alloc_mem

        print('Memory Usage:')
        print(f'  Free: {free_mem} bytes')
        print(f'  Used: {alloc_mem} bytes')
        print(f'  Total: {total_mem} bytes')
        print(f'  Percent used: {alloc_mem / total_mem * 100:.1f}%')
        print('OK')

    def save_log(self, *args):
        if not self.log_manager.enabled:
            print("Logs are disabled")
            print('OK')
            return
        try:
            with open(self.log_manager.filename, "w") as f:
                f.write(self.log_manager.export_logs())
            print(f"Logs saved to {self.log_manager.filename}")
            print('OK')
        except Exception as e:
            print(f"Error saving logs: {e}")
            print('Error: Failed to save logs')

    def exit_app(self, *args):
        self.save_log()
        print('OK')
        self.exit_request = True
        sys.exit(1)

    def receiver_thread(self):
        while True:
            try:
                rx_line = input("> ")
            except KeyboardInterrupt:
                print('Ctrl+C pressed')
                self.exit_app()

            if not rx_line:
                continue

            cmd_parts = rx_line.split()
            if not cmd_parts:
                continue

            cmd = cmd_parts[0]
            params = cmd_parts[1:] if len(cmd_parts) > 1 else []

            try:
                handler = self.commands[cmd]['handler']
            except KeyError:
                print('Error: Unknown command')
            else:
                handler(self, *params)


class LOKO_BLE():
    def __init__(self, name):
        # Create internal objects for the onboard LED_BLUE
        # blinking when no BLE device is connected
        # stable ON when connected
        self.name = name
        self.ble = ubluetooth.BLE()
        self.ble.active(True)
        self.disconnected()
        self.ble.irq(self.ble_irq)
        self.register()
        self.advertiser()
        self.ble.config(gap_name=self.name)
        self.is_connected = False

    def connected(self):
        global BLE_CONNECTED
        BLE_CONNECTED = True
        self.is_connected = True
        blue_solid_ble_connected()
        debug_print('Connected')

    def disconnected(self):
        global BLE_CONNECTED
        BLE_CONNECTED = False
        self.is_connected = False
        blue_blink_ble_wait()
        debug_print('Disconnected')

    def ble_irq(self, event, data):
        if event == 1:  # _IRQ_CENTRAL_CONNECT:
            # A central has connected to this peripheral
            self.connected()
        elif event == 2:  # _IRQ_CENTRAL_DISCONNECT:
            # A central has disconnected from this peripheral.
            self.advertiser()
            self.disconnected()
        elif event == 3:  # _IRQ_GATTS_WRITE:
            ble_msg = self.ble.gatts_read(self.rx).decode('UTF-8').strip()
            debug_print('BLE Rx:', ble_msg)

    def register(self):
        # Nordic UART Service (NUS)
        NUS_UUID = '6E400001-B5A3-F393-E0A9-E50E24DCCA9E'
        RX_UUID = '6E400002-B5A3-F393-E0A9-E50E24DCCA9E'
        TX_UUID = '6E400003-B5A3-F393-E0A9-E50E24DCCA9E'
        BLE_NUS = ubluetooth.UUID(NUS_UUID)
        BLE_RX = (ubluetooth.UUID(RX_UUID), ubluetooth.FLAG_WRITE)
        BLE_TX = (ubluetooth.UUID(TX_UUID), ubluetooth.FLAG_NOTIFY)
        BLE_UART = (BLE_NUS, (BLE_TX, BLE_RX,))
        SERVICES = (BLE_UART, )
        ((self.tx, self.rx,), ) = self.ble.gatts_register_services(SERVICES)

    def send(self, data):
        try:
            self.ble.gatts_notify(0,self.tx, data + '\n')
        except Exception as inst:
            debug_print('BLE Send error:', inst)

    def advertiser(self):
        name = bytes(self.name, 'UTF-8')
        adv_data = bytearray(b'\x02\x01\x02') + \
            bytearray((len(name) + 1, 0x09)) + name
        self.ble.gap_advertise(100, adv_data)
        debug_print('ADV:', adv_data)


def battery_level():
    VBAT_IN.atten(ADC.ATTN_11DB)
    VBAT_IN.width(ADC.WIDTH_12BIT)
    adc_reading = VBAT_IN.read()
    max_adc_value = 2455
    max_battery_voltage = 2.1
    adc_battery_voltage = 2 * (adc_reading * max_battery_voltage / max_adc_value)
    return adc_battery_voltage


def lora_set(freq_hz):
    freq_mhz = freq_hz // 1000000

    LORA_UART.write("AT+MODE=TEST\r\n")
    sleep_ms(1000)
    debug_print('Lora Resp:', LORA_UART.read())
    LORA_UART.write(
        "AT+TEST=RFCFG,{},SF12,125,12,15,14,ON,OFF,OFF\r\n".format(freq_mhz))
    sleep_ms(1000)
    debug_print('Lora Resp:', LORA_UART.read())


def lora_data_receive():
    LORA_UART.write('AT+TEST=RXLRPKT\r\n')
    sleep_ms(500)
    debug_print('Lora RX:', LORA_UART.read())

def is_hex_ascii_convertible(hex_string):
    if not all(c in '0123456789abcdefABCDEF' for c in hex_string):
        return False

    if len(hex_string) % 2 != 0:
        return False

    try:
        decoded_bytes = bytes(int(hex_string[i:i+2], 16) for i in range(0, len(hex_string), 2))
    except ValueError:
        return False

    return all(32 <= byte <= 126 for byte in decoded_bytes)

def parse_lora_module_message(message):
    line = str(message)
    rx_pos = line.find('RX ')
    if rx_pos == -1:
        return None

    first_quote = line.find('"', rx_pos)
    if first_quote == -1:
        return None

    second_quote = line.find('"', first_quote + 1)
    if second_quote == -1:
        return None

    received_data = line[first_quote + 1:second_quote].strip()
    if received_data:
        return received_data
    return None


def parse_loko_string_packet(payload, aes_key):
    try:
        values = payload.split(',')
        if len(values) == 5:
            id1 = int(values[0])
            id2 = int(values[1])
            lat = values[2]
            lon = values[3]
            vbat = int(values[4])
            return {'id1': id1, 'id2': id2, 'lat': lat, 'lon': lon, 'vbat': vbat}

        if len(values) == 7:
            id1 = int(values[0])
            id2 = int(values[1])
            lat = values[2]
            lon = values[3]
            alt_meters = int(values[4])
            meters_per_second = int(values[5])
            vbat = int(values[6])
            return {'id1': id1, 'id2': id2, 'lat': lat, 'lon': lon, 'vbat': vbat, 'alt': alt_meters, 'mps': meters_per_second}

        if len(values) == 3:
            id1 = int(values[0])
            id2 = int(values[1])
            encrypted_bytes = ubinascii.a2b_base64(values[2])
            cipher = aes(aes_key, 1)
            decrypted_bytes = cipher.decrypt(encrypted_bytes)
            if len(decrypted_bytes) != 16:
                return None
            checksum = sum(decrypted_bytes[:-1]) % 256
            lat, lon, vbat_mv, alt_meters, speed_mps, _reserved1, integrity = struct.unpack('<ffHHHBB', decrypted_bytes)
            if checksum != integrity:
                debug_print('Can\'t decrypt, possible wrong key')
                return None
            return {'id1': id1, 'id2': id2, 'lat': lat, 'lon': lon, 'vbat': vbat_mv, 'alt': alt_meters, 'mps': speed_mps}
    except Exception as exc:
        debug_print('Loko string parse error:', exc)

    return None

def bin_unpack_vbat(vbat):
    return (vbat + 27) * 0.1

def bin_unpack_lat_lon_24(packed_data):
    lat_lon_scaled = (packed_data[0] << 16) | (packed_data[1] << 8) | packed_data[2]
    if lat_lon_scaled & 0x800000:
        lat_lon_scaled -= 0x1000000

    scaling_factor = 10000.0
    lat_lon = lat_lon_scaled / scaling_factor

    return lat_lon

def bin_unpack_lat_lon_32(packed_data):
    lat_lon_scaled = (packed_data[0] << 24) | (packed_data[1] << 16) | (packed_data[2] << 8) | packed_data[3]
    if lat_lon_scaled & 0x80000000:
        lat_lon_scaled -= 0x100000000

    scaling_factor = 1000000.0
    lat_lon = lat_lon_scaled / scaling_factor

    return lat_lon

def parse_loko_bin_packet(hex_payload, aes_key):
    try:
        id1 = 0
        id2 = 0
        vbat_mv = 0
        lat = 0.0
        lon = 0.0
        alt_meters = 0
        speed_mps = 0
        data = bytes(int(hex_payload[i:i+2], 16) for i in range(0, len(hex_payload), 2))

        if len(data) == 15:
            id1, id2, vb_version, lat_24bit, lon_24bit = struct.unpack("<IIB3s3s", data)
            vbat_mv = bin_unpack_vbat(vb_version & 0x0F)
            lat = bin_unpack_lat_lon_24(lat_24bit)
            lon = bin_unpack_lat_lon_24(lon_24bit)
        elif len(data) == 17:
            id1, id2, vb_version, lat_32bit, lon_32bit = struct.unpack("<IIB4s4s", data)
            vbat_mv = bin_unpack_vbat(vb_version & 0x0F)
            lat = bin_unpack_lat_lon_32(lat_32bit)
            lon = bin_unpack_lat_lon_32(lon_32bit)
        elif len(data) == 18:
            id1, id2, vb_version, lat_24bit, lon_24bit, speed_mps, alt_meters = struct.unpack("<IIB3s3sBh", data)
            vbat_mv = bin_unpack_vbat(vb_version & 0x0F)
            lat = bin_unpack_lat_lon_24(lat_24bit)
            lon = bin_unpack_lat_lon_24(lon_24bit)
        elif len(data) == 20:
            id1, id2, vb_version, lat_32bit, lon_32bit, speed_mps, alt_meters = struct.unpack("<IIB4s4sBh", data)
            vbat_mv = bin_unpack_vbat(vb_version & 0x0F)
            lat = bin_unpack_lat_lon_32(lat_32bit)
            lon = bin_unpack_lat_lon_32(lon_32bit)
        elif len(data) == 25:
            id1, id2, vb_version, aes_payload = struct.unpack(">IIB16s", data)
            packet_version = (vb_version >> 4) & 0x0F
            encrypted_bytes = bytes(aes_payload)
            cipher = aes(aes_key, 1)
            decrypted_bytes = cipher.decrypt(encrypted_bytes)
            if len(decrypted_bytes) != 16:
                return None
            checksum = sum(decrypted_bytes[:-1]) % 256

            if packet_version == 2:
                payload_vb_version, lat_24bit, lon_24bit, speed_mps, alt_meters, _reserved1, integrity = struct.unpack('<B3s3sBH5sB', decrypted_bytes)
                if checksum != integrity:
                    debug_print('Can\'t decrypt, possible wrong key')
                    return None
                vbat_mv = bin_unpack_vbat(payload_vb_version & 0x0F)
                lat = bin_unpack_lat_lon_24(lat_24bit)
                lon = bin_unpack_lat_lon_24(lon_24bit)
            elif packet_version == 5:
                payload_vb_version, lat_32bit, lon_32bit, speed_mps, alt_meters, _reserved1, integrity = struct.unpack('<B4s4sBH3sB', decrypted_bytes)
                if checksum != integrity:
                    debug_print('Can\'t decrypt, possible wrong key')
                    return None
                vbat_mv = bin_unpack_vbat(payload_vb_version & 0x0F)
                lat = bin_unpack_lat_lon_32(lat_32bit)
                lon = bin_unpack_lat_lon_32(lon_32bit)
            else:
                return None
        else:
            return None

        return {'id1': id1, 'id2': id2, 'lat': lat, 'lon': lon, 'vbat': vbat_mv, 'alt': alt_meters, 'mps': speed_mps}
    except Exception as exc:
        debug_print('Loko binary parse error:', exc)
        return None


def format_loko_packet(loko_data):
    parts = [
        str(loko_data['id1']),
        str(loko_data['id2']),
        str(loko_data['lat']),
        str(loko_data['lon']),
        str(loko_data['vbat']),
    ]
    if loko_data.get('alt') is not None and loko_data.get('mps') is not None:
        parts.append(str(loko_data['alt']))
        parts.append(str(loko_data['mps']))
    return ','.join(parts)

def shutdown_device(reason):
    print(reason)
    red_solid_shutdown()
    sleep_ms(100)
    POWER_CTRL.value(0)
    try:
        machine.deepsleep()
    except Exception:
        sys.exit(0)

# LEDs are active-low: 0 = ON, 1 = OFF
def led_off_all():
    if LEDS is not None:
        LEDS.off_all()

def blue_blink_ble_wait():
    if LEDS is not None:
        LEDS.blue_waiting()

def blue_solid_ble_connected():
    if VERBOSE_MODE and LEDS is not None:
        LEDS.blue_connected()

def green_blink_rx():
    if VERBOSE_MODE and LEDS is not None:
        LEDS.green_pulse()

def green_double_blink_forwarded():
    if VERBOSE_MODE and LEDS is not None:
        LEDS.green_double_blink()

def red_blink_error():
    if LEDS is not None:
        LEDS.red_error_pulse()

def red_low_battery_pulse():
    if LEDS is not None:
        LEDS.red_pulse()

def red_solid_shutdown():
    if LEDS is not None:
        LEDS.red_solid()


def handle_button_input(now_ms, button_state, pressed):
    if pressed == 0:
        if button_state['press_started_at'] is None:
            button_state['press_started_at'] = now_ms
        elif not button_state['shutdown_armed'] and ticks_diff(now_ms, button_state['press_started_at']) >= BUTTON_HOLD_MS:
            button_state['shutdown_armed'] = True
            red_solid_shutdown()
            debug_print("Shutdown armed; release button to power off")
        return

    if button_state['shutdown_armed']:
        shutdown_device("Long press detected, shutting down")
        return

    if button_state['press_started_at'] is None:
        return

    press_duration = ticks_diff(now_ms, button_state['press_started_at'])
    button_state['press_started_at'] = None
    if press_duration < BUTTON_CLICK_DEBOUNCE_MS:
        return

    if button_state['click_count'] == 1 and ticks_diff(now_ms, button_state['click_deadline_at']) < 0:
        toggle_verbose_mode()
        button_state['click_count'] = 0
        button_state['click_deadline_at'] = 0
        return

    button_state['click_count'] = 1
    button_state['click_deadline_at'] = now_ms + BUTTON_DOUBLE_CLICK_MS


def process_button_events(now_ms, button_state):
    global BUTTON_IRQ_TAIL
    while BUTTON_IRQ_TAIL != BUTTON_IRQ_HEAD:
        idx = BUTTON_IRQ_TAIL
        event_time = BUTTON_IRQ_TIMES[idx]
        event_state = BUTTON_IRQ_STATES[idx]
        BUTTON_IRQ_TAIL = (BUTTON_IRQ_TAIL + 1) % BUTTON_IRQ_QUEUE_SIZE
        handle_button_input(event_time, button_state, event_state)

    if button_state['click_count'] == 1 and ticks_diff(now_ms, button_state['click_deadline_at']) >= 0:
        show_verbose_mode_status()
        button_state['click_count'] = 0
        button_state['click_deadline_at'] = 0

def main():
    global LEDS
    debug_print(battery_level())

    LEDS = LED_MANAGER(LED_BLUE, LED_RED, LED_GREEN)

    LED_BLUE.value(0)
    sleep_ms(500)
    LED_BLUE.value(1)
    LED_RED.value(0)
    sleep_ms(500)
    LED_RED.value(1)
    LED_GREEN.value(0)
    sleep_ms(500)
    LED_GREEN.value(1)
    POWER_CTRL.value(1)
    LED_GREEN.value(0)
    sleep_ms(500)
    LED_GREEN.value(1)

    log_manager = LOG_MANAGER(max_entries=LOG_MAX_ENTRIES, filename=LOG_FILENAME, enabled=LOG_ENABLED)
    debug_print("Log manager initialized")

    settings = SETTINGS()
    command_parser = None
    if use_command_line_parser:
        command_parser = COMMAND_RECEIVER(settings, log_manager)

    aes_key = bytes(int(settings.data['p2p_key'][i:i+2], 16) for i in range(0, len(settings.data['p2p_key']), 2))

    ble = LOKO_BLE("LOKO")
    lora_set(settings.data['freq'])
    lora_data_receive()
    BUTTON.irq(trigger=Pin.IRQ_FALLING | Pin.IRQ_RISING, handler=button_irq)
    btCounter = 0
    button_state = {
        'press_started_at': None,
        'shutdown_armed': False,
        'click_count': 0,
        'click_deadline_at': 0,
    }
    low_battery_state = {
        'tier': 0,
        'next_pulse_at': 0,
    }

    while True:
        sleep_ms(MAIN_LOOP_SLEEP_MS)
        current_battery = battery_level()
        now_ms = ticks_ms()
        process_button_events(now_ms, button_state)

        if LEDS is not None:
            LEDS.update()

        if current_battery < LOW_BATTERY_SHUTDOWN_V:
            red_solid_shutdown()
            shutdown_device("Battery level too low. Device entering deep sleep to protect from overcharge.")
        elif current_battery < LOW_BATTERY_CRITICAL_V:
            if low_battery_state['tier'] != 2:
                low_battery_state['tier'] = 2
                low_battery_state['next_pulse_at'] = now_ms
                debug_print("Battery critical; closing in on shutdown")
            if ticks_diff(now_ms, low_battery_state['next_pulse_at']) >= 0:
                red_low_battery_pulse()
                low_battery_state['next_pulse_at'] = now_ms + LOW_BATTERY_CRITICAL_INTERVAL_MS
        elif current_battery < LOW_BATTERY_THRESHOLD_V:
            if low_battery_state['tier'] != 1:
                low_battery_state['tier'] = 1
                low_battery_state['next_pulse_at'] = now_ms
                debug_print("Battery low; warning cadence enabled")
            if ticks_diff(now_ms, low_battery_state['next_pulse_at']) >= 0:
                red_low_battery_pulse()
                low_battery_state['next_pulse_at'] = now_ms + LOW_BATTERY_WARNING_INTERVAL_MS
        else:
            low_battery_state['tier'] = 0
            low_battery_state['next_pulse_at'] = 0

        if use_command_line_parser and command_parser is not None:
            if command_parser.exit_request:
               sys.exit(1)

        if btCounter < BT_BROADCAST_INTERVAL + 1:
            if ble.is_connected:
                battery_percent = (current_battery - LOW_BATTERY_THRESHOLD_V) * 100 / 0.9
                battery_str = str(round(battery_percent, 2))
                ble.send(battery_str)
        btCounter += 1
        if btCounter > BT_BROADCAST_INTERVAL:
            btCounter = 0

        lora_data = LORA_UART.read()
        if lora_data is None:
            continue
        green_blink_rx()
        debug_print('LoraRx: ', lora_data)

        loko_payload = parse_lora_module_message(lora_data)
        if loko_payload is None:
            continue
        loko_data = None
        if is_hex_ascii_convertible(loko_payload):
            converted_data = ubinascii.unhexlify(loko_payload)
            raw_loko_string = converted_data.decode("utf-8")
            debug_print('LokoMessage: ', raw_loko_string)

            loko_data = parse_loko_string_packet(raw_loko_string, aes_key)
        else:
            loko_data = parse_loko_bin_packet(loko_payload, aes_key)
            if loko_data is None:
                continue
            debug_print('LokoMessage: ', format_loko_packet(loko_data))

        if loko_data is None:
            continue
        loko_string = format_loko_packet(loko_data)

        if use_command_line_parser and command_parser is not None:
            log_entry = f"ID1={loko_data['id1']}, ID2={loko_data['id2']}, LAT={loko_data['lat']}, LON={loko_data['lon']}, VBAT={loko_data['vbat']}"
            if loko_data.get('alt') is not None and loko_data.get('mps') is not None:
                log_entry += f", ALT={loko_data['alt']}, MPS={loko_data['mps']}"
            command_parser.log_manager.add_entry(log_entry)

        if loko_data['id2'] == settings.data['id2']:
            green_double_blink_forwarded()
            if ble.is_connected:
                ble.send(loko_string)
            else:
                debug_print('BLE not connected')
        else:
            red_blink_error()
            debug_print('DEBUG:Received unexpected ID2={}, Expected={}'.format(
                loko_data['id2'], settings.data['id2']))

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Ctrl+C pressed, exit from application')
        exit(1)
