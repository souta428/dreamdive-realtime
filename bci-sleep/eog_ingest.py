# eog_ingest.py
import threading, socket, json, time
from queue import Queue

class UDPJsonEOGSource:
    """
    送信例:
      {"t": 1724966400.123, "eog": 12.3}
    を UDP で連続送出（例: 200Hz）。受信側は50Hzに間引いて使用。
    """
    def __init__(self, host="0.0.0.0", port=9000, bufsize=65536, timeout=1.0):
        self.host, self.port = host, port
        self.bufsize, self.timeout = bufsize, timeout
        self.q = Queue()
        self._stop = threading.Event()

    def start(self):
        self.th = threading.Thread(target=self._run, daemon=True)
        self.th.start()
        return self.q

    def stop(self): self._stop.set()

    def _run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((self.host, self.port))
        sock.settimeout(self.timeout)
        while not self._stop.is_set():
            try:
                data, _ = sock.recvfrom(self.bufsize)
                obj = json.loads(data.decode("utf-8"))
                t = float(obj.get("t", time.time()))
                v = float(obj.get("eog", 0.0))
                self.q.put((t, v))
            except socket.timeout:
                continue
            except Exception:
                continue
        sock.close()


class SerialCSVEOGSource:
    """
    シリアル/USB 送信例:
      "1724966400.123,12.3\n"
    のように "timestamp,value" で連続送出。
    """
    def __init__(self, port="/dev/ttyUSB0", baud=115200):
        import serial  # requirements.txt に pyserial を記載
        self.serial = serial.Serial(port, baudrate=baud, timeout=1)
        self.q = Queue()
        self._stop = threading.Event()

    def start(self):
        self.th = threading.Thread(target=self._run, daemon=True)
        self.th.start()
        return self.q

    def stop(self): self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                line = self.serial.readline().decode("utf-8").strip()
                if not line: continue
                t_str, v_str = line.split(",")
                self.q.put((float(t_str), float(v_str)))
            except Exception:
                continue
        self.serial.close()
