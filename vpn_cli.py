import json
import base64
import urllib.parse
from urllib.parse import urlparse, parse_qs
import subprocess
import os
import time
import requests
import socket
import random
import concurrent.futures
from tqdm import tqdm # استفاده از tqdm برای نوار پیشرفت در CLI
import threading
import queue
import sys
from datetime import datetime

# --- تابع کمکی سراسری برای کشتن فرآیندهای Xray ---
def kill_xray_processes_termux():
    """فرآیندهای Xray موجود را در Termux/Linux از بین می‌برد."""
    try:
        # استفاده از pkill برای محیط Termux/Linux
        subprocess.run(['pkill', '-f', 'xray'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        # در اینجا از print استفاده می‌کنیم چون این تابع قبل از راه‌اندازی سیستم لاگینگ کلاس فراخوانی می‌شود.
        print(f"[ERROR] Error killing existing Xray processes: {str(e)}")

# --- کلاس VPNConfigCLI ---
class VPNConfigCLI:
    def __init__(self):
        # هنگام راه‌اندازی، فرآیندهای Xray موجود را از بین می‌برد
        kill_xray_processes_termux()
        
        self.stop_event = threading.Event()
        self.thread_lock = threading.Lock()
        self.active_threads = []

        # پیکربندی - اکنون از یک دیکشنری از میرورها استفاده می‌کند
        self.MIRRORS = {
            "config_proxy": "https://raw.githubusercontent.com/proco2024/channel/main/Telegram%3A%40config_proxy-14040412-007.txt",
            "config_proxy4": "https://raw.githubusercontent.com/hamedp-71/Trojan/refs/heads/main/hp.txt",
            "barry-far": "https://raw.githubusercontent.com/barry-far/V2ray-Config/refs/heads/main/All_Configs_Sub.txt",
        }
        self.CONFIGS_URL = self.MIRRORS["barry-far"]  # میرور پیش‌فرض

        self.WORKING_CONFIGS_FILE = "working_configs.txt"
        self.BEST_CONFIGS_FILE = "best_configs.txt"
        self.TEMP_FOLDER = os.path.join(os.getcwd(), "temp_xray_configs")
        
        # مسیر Xray را به 'xray' تنظیم می‌کنیم، زیرا انتظار می‌رود در PATH سیستم باشد
        # پس از نصب با 'pkg install xray' در Termux
        self.XRAY_PATH = "xray" 
        
        self.TEST_TIMEOUT = 10
        self.SOCKS_PORT = 1080
        self.PING_TEST_URL = "https://www.google.com/generate_204"  
        self.LATENCY_WORKERS = 100 # تعداد ترد پیش‌فرض

        # پوشه موقت را ایجاد می‌کند اگر وجود نداشته باشد
        if not os.path.exists(self.TEMP_FOLDER):
            os.makedirs(self.TEMP_FOLDER)

        # متغیرها
        self.best_configs = [] # (config_uri, latency) را ذخیره می‌کند
        self.log_queue = queue.Queue() # برای لاگ‌های داخلی
        self.total_configs = 0
        self.tested_configs = 0
        self.working_configs = 0
        
        self.setup_logging() # راه‌اندازی سیستم لاگینگ

    def clear_temp_folder(self):
        """تمام فایل‌های موجود در پوشه موقت را پاک می‌کند."""
        try:
            for filename in os.listdir(self.TEMP_FOLDER):
                file_path = os.path.join(self.TEMP_FOLDER, filename)
                try:
                    if os.path.isfile(file_path):
                        os.unlink(file_path)
                except Exception as e:
                    self.log(f"Failed to delete {file_path}: {e}")
        except Exception as e:
            self.log(f"Error clearing temp folder: {e}")

    def fetch_and_test_configs(self):
        """تابع اصلی برای دریافت و تست کانفیگ‌ها."""
        self.log("Starting config fetch and test...")
        self.stop_event.clear() # هر وضعیت توقف قبلی را پاک می‌کند

        try:
            # دریافت کانفیگ‌ها
            self.log("Fetching configs from GitHub...")
            configs = self.fetch_configs()
            if not configs or self.stop_event.is_set():
                self.log("Operation stopped or no configs found")
                return
                
            self.total_configs = len(configs)
            self.tested_configs = 0
            self.working_configs = 0
            self.log(f"Found {len(configs)} configs to test")
            
            # بارگذاری کانفیگ‌های برتر موجود برای جلوگیری از ذخیره مجدد موارد تکراری
            existing_configs = set()
            if os.path.exists(self.BEST_CONFIGS_FILE):
                with open(self.BEST_CONFIGS_FILE, 'r', encoding='utf-8') as f:
                    existing_configs = {line.strip() for line in f if line.strip()}
            
            # تست کانفیگ‌ها برای تأخیر
            self.log("Testing configs for latency...")
            best_configs_current_run = [] # برای ذخیره کانفیگ‌های فعال جدید از این اجرا
            
            # استفاده از tqdm برای نوار پیشرفت در کنسول
            with tqdm(total=self.total_configs, desc="Testing progress") as pbar:
                with concurrent.futures.ThreadPoolExecutor(max_workers=self.LATENCY_WORKERS) as executor:
                    futures = {executor.submit(self.measure_latency, config): config for config in configs}
                    for future in concurrent.futures.as_completed(futures):
                        if self.stop_event.is_set():
                            # لغو تمام futuresهای در حال انتظار اگر رویداد توقف تنظیم شده باشد
                            for f in futures:
                                f.cancel()
                            break
                            
                        result_uri, result_latency = future.result()
                        self.tested_configs += 1
                        pbar.update(1) # به‌روزرسانی نوار پیشرفت
                        
                        if result_latency != float('inf'):
                            # بررسی می‌کند که آیا کانفیگ قبلاً در best_configs_current_run یا existing_configs نیست
                            config_uri = result_uri
                            if (not any(x[0] == config_uri for x in best_configs_current_run) and
                                config_uri not in existing_configs):
                                    
                                best_configs_current_run.append((config_uri, result_latency))
                                self.working_configs += 1
                                # برای جلوگیری از تکرار در طول اجرا، به مجموعه کانفیگ‌های موجود اضافه می‌کند
                                existing_configs.add(config_uri)  
                                    
                                # بلافاصله به BEST_CONFIGS_FILE اضافه می‌کند
                                with open(self.BEST_CONFIGS_FILE, 'a', encoding='utf-8') as f:
                                    f.write(f"{config_uri}\n")
                                    
                                self.log(f"Working config found: {result_latency:.2f}ms - added to best configs file")
                                
                        pbar.set_postfix(Working=self.working_configs, Tested=self.tested_configs)

            # لیست داخلی best_configs را بر اساس تأخیر مرتب می‌کند (اختیاری برای CLI، اما برای سازگاری داخلی خوب است)
            self.best_configs.extend(best_configs_current_run)
            self.best_configs.sort(key=lambda x: x[1])
            
            # تمام کانفیگ‌های فعال را در working_configs.txt ذخیره می‌کند (برای اشکال‌زدایی/بررسی)
            working_uris = [uri for uri, _ in self.best_configs if _ != float('inf')]
            with open(self.WORKING_CONFIGS_FILE, "w", encoding='utf-8') as f:
                f.write("\n".join(working_uris))
                
            self.log(f"Testing complete! Found {len(working_uris)} working configs. See {self.BEST_CONFIGS_FILE}")
            
        except Exception as e:
            if not self.stop_event.is_set():
                self.log(f"Error in fetch and test: {str(e)}")
        finally:
            self.clear_temp_folder()
            self.log("Operation finished.")

    # --- متدهای تجزیه کانفیگ (همانند اصلی) ---
    def parse_config_info(self, config_uri):
        """اطلاعات اولیه را از URI کانفیگ استخراج می‌کند."""
        try:
            if config_uri.startswith("vmess://"):
                base64_str = config_uri[8:]
                padded = base64_str + '=' * (4 - len(base64_str) % 4)
                decoded = base64.urlsafe_b64decode(padded).decode('utf-8')
                vmess_config = json.loads(decoded)
                return "vmess", vmess_config.get("add", "unknown"), vmess_config.get("port", "unknown")
            elif config_uri.startswith("vless://"):
                parsed = urllib.parse.urlparse(config_uri)
                return "vless", parsed.hostname or "unknown", parsed.port or "unknown"
            elif config_uri.startswith("ss://"):
                # Shadowsocks URIs پیچیده هستند، فعلاً فقط یک اطلاعات عمومی برمی‌گردانیم
                return "shadowsocks", "unknown", "unknown"  
            elif config_uri.startswith("trojan://"):
                parsed = urllib.parse.urlparse(config_uri)
                return "trojan", parsed.hostname or "unknown", parsed.port or "unknown"
        except:
            pass
        return "unknown", "unknown", "unknown"
        
    def vmess_to_json(self, vmess_url):
        if not vmess_url.startswith("vmess://"):
            raise ValueError("Invalid VMess URL format")
        
        base64_str = vmess_url[8:]
        padded = base64_str + '=' * (4 - len(base64_str) % 4)
        decoded_bytes = base64.urlsafe_b64decode(padded)
        decoded_str = decoded_bytes.decode('utf-8')
        vmess_config = json.loads(decoded_str)
        
        xray_config = {
            "inbounds": [{
                "port": self.SOCKS_PORT,
                "listen": "127.0.0.1",
                "protocol": "socks",
                "settings": {"udp": True}
            }],
            "outbounds": [{
                "protocol": "vmess",
                "settings": {
                    "vnext": [{
                        "address": vmess_config["add"],
                        "port": int(vmess_config["port"]),
                        "users": [{
                            "id": vmess_config["id"],
                            "alterId": int(vmess_config.get("aid", 0)),
                            "security": vmess_config.get("scy", "auto")
                        }]
                    }]
                },
                "streamSettings": {
                    "network": vmess_config.get("net", "tcp"),
                    "security": vmess_config.get("tls", ""),
                    "tcpSettings": {
                        "header": {
                            "type": vmess_config.get("type", "none"),
                            "request": {
                                "path": [vmess_config.get("path", "/")],
                                "headers": {
                                    "Host": [vmess_config.get("host", "")]
                                }
                            }
                        }
                    } if vmess_config.get("net") == "tcp" and vmess_config.get("type") == "http" else None
                }
            }]
        }
        
        if not xray_config["outbounds"][0]["streamSettings"]["security"]:
            del xray_config["outbounds"][0]["streamSettings"]["security"]
        if not xray_config["outbounds"][0]["streamSettings"].get("tcpSettings"):
            xray_config["outbounds"][0]["streamSettings"].pop("tcpSettings", None)
            
        return xray_config

    def parse_vless(self, uri):
        parsed = urllib.parse.urlparse(uri)
        config = {
            "inbounds": [{
                "port": self.SOCKS_PORT,
                "listen": "127.0.0.1",
                "protocol": "socks",
                "settings": {"udp": True}
            }],
            "outbounds": [{
                "protocol": "vless",
                "settings": {
                    "vnext": [{
                        "address": parsed.hostname,
                        "port": parsed.port,
                        "users": [{
                            "id": parsed.username,
                            "encryption": parse_qs(parsed.query).get("encryption", ["none"])[0]
                        }]
                    }]
                },
                "streamSettings": {
                    "network": parse_qs(parsed.query).get("type", ["tcp"])[0],
                    "security": parse_qs(parsed.query).get("security", ["none"])[0]
                }
            }]
        }
        return config

    def parse_shadowsocks(self, uri):
        if not uri.startswith("ss://"):
            raise ValueError("Invalid Shadowsocks URI")
            
        parts = uri[5:].split("#", 1)
        encoded_part = parts[0]
        # remark = urllib.parse.unquote(parts[1]) if len(parts) > 1 else "Imported Shadowsocks" # در کانفیگ استفاده نمی‌شود

        if "@" in encoded_part:
            userinfo, server_part = encoded_part.split("@", 1)
        else:
            decoded = base64.b64decode(encoded_part + '=' * (-len(encoded_part) % 4)).decode('utf-8')
            if "@" in decoded:
                userinfo, server_part = decoded.split("@", 1)
            else:
                userinfo = decoded
                server_part = ""

        if ":" in server_part:
            server, port = server_part.rsplit(":", 1)
            port = int(port)
        else:
            server = server_part
            port = 443 # پورت پیش‌فرض SS

        try:
            decoded_userinfo = base64.b64decode(userinfo + '=' * (-len(userinfo) % 4)).decode('utf-8')
        except:
            decoded_userinfo = base64.b64decode(encoded_part + '=' * (-len(encoded_part) % 4)).decode('utf-8')
            if "@" in decoded_userinfo:
                userinfo_part, server_part_decoded = decoded_userinfo.split("@", 1)
                if ":" in server_part_decoded:
                    server, port = server_part_decoded.rsplit(":", 1)
                    port = int(port)
                decoded_userinfo = userinfo_part

        if ":" not in decoded_userinfo:
            raise ValueError("Invalid Shadowsocks URI - missing method:password")
            
        method, password = decoded_userinfo.split(":", 1)

        config = {
            "inbounds": [{
                "port": self.SOCKS_PORT,
                "listen": "127.0.0.1",
                "protocol": "socks",
                "settings": {"udp": True}
            }],
            "outbounds": [
                {
                    "protocol": "shadowsocks",
                    "settings": {
                        "servers": [{
                            "address": server,
                            "port": port,
                            "method": method,
                            "password": password
                        }]
                    },
                    "tag": "proxy"
                },
                {
                    "protocol": "freedom",
                    "tag": "direct"
                }
            ],
            "routing": {
                "domainStrategy": "IPOnDemand",
                "rules": [{
                    "type": "field",
                    "ip": ["geoip:private"],
                    "outboundTag": "direct"
                }]
            }
        }
        
        return config

    def parse_trojan(self, uri):
        if not uri.startswith("trojan://"):
            raise ValueError("Invalid Trojan URI")
            
        parsed = urllib.parse.urlparse(uri)
        password = parsed.username
        server = parsed.hostname
        port = parsed.port
        query = parse_qs(parsed.query)
        # remark = urllib.parse.unquote(parsed.fragment) if parsed.fragment else "Imported Trojan" # در کانفیگ استفاده نمی‌شود
            
        config = {
            "inbounds": [{
                "port": self.SOCKS_PORT,
                "listen": "127.0.0.1",
                "protocol": "socks",
                "settings": {"udp": True}
            }],
            "outbounds": [
                {
                    "protocol": "trojan",
                    "settings": {
                        "servers": [{
                            "address": server,
                            "port": port,
                            "password": password
                        }]
                    },
                    "streamSettings": {
                        "network": query.get("type", ["tcp"])[0],
                        "security": "tls",
                        "tcpSettings": {
                            "header": {
                                "type": query.get("headerType", ["none"])[0],
                                "request": {
                                    "headers": {
                                        "Host": [query.get("host", [""])[0]]
                                    }
                                }
                            }
                        }
                    },
                    "tag": "proxy"
                },
                {
                    "protocol": "freedom",
                    "tag": "direct"
                }
            ],
            "routing": {
                "domainStrategy": "IPOnDemand",
                "rules": [{
                    "type": "field",
                    "ip": ["geoip:private"],
                    "outboundTag": "direct"
                }]
            }
        }
        
        return config

    def parse_protocol(self, uri):
        """یک URI کانفیگ را تجزیه کرده و پیکربندی JSON Xray مربوطه را برمی‌گرداند."""
        if uri.startswith("vmess://"):
            return self.vmess_to_json(uri)
        elif uri.startswith("vless://"):
            return self.parse_vless(uri)
        elif uri.startswith("ss://"):
            return self.parse_shadowsocks(uri)
        elif uri.startswith("trojan://"):
            return self.parse_trojan(uri)
        raise ValueError("Unsupported protocol: " + uri[:10] + "...")

    def is_port_available(self, port):
        """بررسی می‌کند که آیا یک پورت در دسترس است یا خیر."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('127.0.0.1', port))
                return True
            except socket.error:
                return False

    def get_available_port(self):
        """یک پورت تصادفی در دسترس را پیدا می‌کند."""
        for _ in range(10):
            port = random.randint(49152, 65535) # محدوده پورت‌های موقت
            if self.is_port_available(port):
                return port
        self.log("[WARNING] Could not find a random available port, trying default 1080.")
        if self.is_port_available(1080):
            return 1080
        return None # نشان می‌دهد که پورتی در دسترس یافت نشد

    def measure_latency(self, config_uri):
        """تأخیر یک URI کانفیگ داده شده را اندازه‌گیری می‌کند."""
        if self.stop_event.is_set():
            return (config_uri, float('inf'))
            
        temp_config_file = None # مقداردهی اولیه به None برای بلوک finally
        xray_process = None # مقداردهی اولیه به None برای بلوک finally
        
        try:
            socks_port = self.get_available_port()
            if socks_port is None:
                self.log(f"[ERROR] No available port found for config: {config_uri[:50]}...")
                return (config_uri, float('inf'))
                
            config = self.parse_protocol(config_uri)
            config['inbounds'][0]['port'] = socks_port
            
            rand_suffix = random.randint(100000, 999999)
            temp_config_file = os.path.join(self.TEMP_FOLDER, f"temp_config_{rand_suffix}.json")
            
            with open(temp_config_file, "w", encoding='utf-8') as f:
                json.dump(config, f)
                
            # برای Termux/Linux، نیازی به CREATE_NO_WINDOW یا startupinfo نیست.
            # برای اشکال‌زدایی بهتر، تغییر مسیر stdout/stderr حذف شده است.
            xray_process = subprocess.Popen(
                [self.XRAY_PATH, "run", "-config", temp_config_file]
            )
            
            # به Xray اجازه می‌دهد تا مقداردهی اولیه شود
            time.sleep(0.5) # زمان خواب کمی افزایش یافته است
            
            # بررسی رویداد توقف قبل از ادامه
            if self.stop_event.is_set():
                if xray_process: xray_process.terminate()
                try: os.remove(temp_config_file)
                except: pass
                return (config_uri, float('inf'))
                
            proxies = {
                'http': f'socks5://127.0.0.1:{socks_port}',
                'https': f'socks5://127.0.0.1:{socks_port}'
            }
            
            latency = float('inf')
            try:
                start_time = time.perf_counter()
                response = requests.get(
                    self.PING_TEST_URL,
                    proxies=proxies,
                    timeout=7, # MODIFIED: زمان‌بندی برای پایداری افزایش یافته است
                    headers={
                        'Cache-Control': 'no-cache',
                        'Connection': 'close'
                    }
                )
                if response.status_code == 200 or response.status_code == 204: # 204 برای google.com/generate_204
                    latency = (time.perf_counter() - start_time) * 1000
            except requests.RequestException as req_e:
                self.log(f"[ERROR] Request error for {config_uri[:50]}... (Port {socks_port}): {req_e}") # UNCOMMENTED & IMPROVED LOGGING
            finally:
                if xray_process:
                    xray_process.terminate()
                    try:
                        xray_process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        xray_process.kill()
            
        except Exception as e:
            self.log(f"[ERROR] Latency test failed for {config_uri[:50]}... (General Error): {str(e)}") # UNCOMMENTED & IMPROVED LOGGING
            latency = float('inf')
        finally:
            # اطمینان از حذف فایل موقت
            if temp_config_file and os.path.exists(temp_config_file):
                try:
                    os.remove(temp_config_file)
                except Exception as e:
                    self.log(f"[ERROR] Failed to clean up temp file {temp_config_file}: {e}")
            time.sleep(0.05) # تأخیر کوچک برای جلوگیری از بار زیاد روی سیستم

        return (config_uri, latency)
        
    def fetch_configs(self):
        """کانفیگ‌ها را از URL مشخص شده دریافت می‌کند."""
        try:
            response = requests.get(self.CONFIGS_URL)
            response.raise_for_status()
            response.encoding = 'utf-8'  # صراحتاً کدگذاری UTF-8 را تنظیم می‌کند
            configs = [line.strip() for line in response.text.splitlines() if line.strip()]
            return configs[::-1]  # لیست را قبل از بازگشت معکوس می‌کند
        except Exception as e:
            self.log(f"[ERROR] Failed to fetch configs: {str(e)}")
            return []

def main():
    # فرآیندهای Xray موجود را قبل از شروع از بین می‌برد
    kill_xray_processes_termux()
    
    app = VPNConfigCLI()
    
    print("\n--- VPN Config CLI Manager ---")
    print("این ابزار کانفیگ‌های VPN را دریافت کرده، تأخیر آن‌ها را تست کرده و موارد فعال را ذخیره می‌کند.")
    print("کانفیگ‌ها در 'best_configs.txt' ذخیره خواهند شد.")
    print("برای توقف فرآیند در هر زمان، Ctrl+C را فشار دهید.")

    try:
        # به کاربر اجازه می‌دهد تا میرور و تعداد ترد را از طریق ورودی انتخاب کند (گفتگوی CLI ساده)
        print("\nمیرورهای موجود:")
        for i, mirror_name in enumerate(app.MIRRORS.keys()):
            print(f"{i+1}. {mirror_name}")
        
        while True:
            try:
                mirror_choice = input(f"یک میرور را انتخاب کنید (1-{len(app.MIRRORS)}) یا برای پیش‌فرض ('{list(app.MIRRORS.keys())[0]}') Enter را فشار دهید: ").strip()
                if not mirror_choice:
                    selected_mirror_name = list(app.MIRRORS.keys())[0] # پیش‌فرض به اولین میرور
                    break
                
                mirror_index = int(mirror_choice) - 1
                if 0 <= mirror_index < len(app.MIRRORS):
                    selected_mirror_name = list(app.MIRRORS.keys())[mirror_index]
                    break
                else:
                    print("انتخاب نامعتبر. لطفاً عددی در محدوده را وارد کنید.")
            except ValueError:
                print("ورودی نامعتبر. لطفاً یک عدد وارد کنید.")
        
        app.CONFIGS_URL = app.MIRRORS[selected_mirror_name]
        print(f"در حال استفاده از میرور: {selected_mirror_name} ({app.CONFIGS_URL})")

        while True:
            try:
                thread_choice = input("تعداد تردها برای تست را وارد کنید (مثلاً 10، 20، 50، 100) یا برای پیش‌فرض (100) Enter را فشار دهید: ").strip()
                if not thread_choice:
                    app.LATENCY_WORKERS = 100 # ترد پیش‌فرض
                    break
                
                num_threads = int(thread_choice)
                if num_threads > 0:
                    app.LATENCY_WORKERS = num_threads
                    break
                else:
                    print("تعداد تردها باید مثبت باشد.")
            except ValueError:
                print("ورودی نامعتبر. لطفاً یک عدد وارد کنید.")

        print(f"در حال استفاده از {app.LATENCY_WORKERS} ترد برای تست.")
        
        # فرآیند دریافت و تست را در یک ترد پس‌زمینه شروع می‌کند
        test_thread = threading.Thread(target=app.fetch_and_test_configs, daemon=True)
        test_thread.start()

        # ترد اصلی را فعال نگه می‌دارد تا ترد‌های پس‌زمینه اجرا شوند
        # این همچنین Ctrl+C را برای خاتمه graceful می‌گیرد
        while test_thread.is_alive():
            time.sleep(1)

    except KeyboardInterrupt:
        print("\n[INFO] Ctrl+C شناسایی شد. در حال توقف تمام عملیات...")
        app.stop_event.set() # سیگنال توقف به ترد‌ها
        # به ترد‌ها فرصت می‌دهد تا به رویداد توقف واکنش نشان دهند
        time.sleep(2)  
        kill_xray_processes_termux() # اطمینان از کشتن فرآیندهای Xray
        print("[INFO] اپلیکیشن خاتمه یافت.")
    except Exception as e:
        print(f"[CRITICAL ERROR] یک خطای غیرمنتظره رخ داد: {str(e)}")
        kill_xray_processes_termux() # اطمینان از کشتن فرآیندهای Xray

if __name__ == "__main__":
    main()
