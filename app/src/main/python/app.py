# -*- coding: utf-8 -*-
"""
الخادم الخلفي (Flask) لتطبيق "إضافة موظفين".

نسخة مصغّرة من تطبيق إدارة أجهزة البصمة، وظيفتها واحدة: استيراد موظفين
وبصماتهم من ملف إلى جهاز واحد. مسار الاستيراد منقول حرفيًا من التطبيق
الأصلي بعد اختباره على أجهزة حقيقية:
  - الاتصال عبر TCP أولًا ثم UDP احتياطًا (UDP يضيّع البيانات بصمت).
  - قراءة قائمة الموظفين والتحقق من اكتمالها قبل أي كتابة.
  - تعطيل الجهاز أثناء الكتابة وإعادة تفعيله بعدها.
  - إعادة قراءة كل بصمة بعد كتابتها ومقارنتها بايتًا ببايت.

قرارات مثبّتة عمدًا (لا يراها المستخدم ولا يستطيع تغييرها):
  - لا كتابة فوق الموظفين الموجودين إطلاقًا.
  - البصمات تُنقل دائمًا.
  - لا يوجد أمر إعادة تشغيل للجهاز في هذا التطبيق أصلًا.
"""

from flask import Flask, jsonify, request, send_from_directory
from datetime import datetime
from zk import ZK
import os
import json
import re
import socket
import threading
import time
import uuid

app = Flask(__name__, static_folder=None)

BASE_DIR = os.path.expanduser("~/zk_import_app")
DEVICE_FILE = "device.json"

CONNECT_TIMEOUT = 1.5         # فحص اتصال سريع (ثوانٍ)
ZK_TIMEOUT = 5
IMPORT_TIMEOUT = 20           # الكتابة أبطأ من القراءة، خصوصًا مع القوالب
MAX_EMPLOYEE_NUMBER_BYTES = 24
MAX_DEVICE_UID = 65535
EMPLOYEES_FILE_FORMAT = "zkcontrol-employees"

# الأرقام العربية-الهندية (٠-٩ والفارسية ۰-۹) محارف مختلفة تمامًا عن 0-9
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
# محارف اتجاه خفية يدسّها إكسل وبعض المحررات - غير مرئية لكنها تكسر المقارنة
_INVISIBLE_CHARS = dict.fromkeys(
    [0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x202A, 0x202B, 0x202C,
     0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069, 0xFEFF], None)

_device_locks = {}
_device_locks_guard = threading.Lock()


def _path(name):
    return os.path.join(BASE_DIR, name)


def load_device():
    """عنوان الجهاز المحفوظ - يُكتب مرة واحدة ولا يُعاد إدخاله كل مرة."""
    try:
        with open(_path(DEVICE_FILE), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_device(ip):
    os.makedirs(BASE_DIR, exist_ok=True)
    with open(_path(DEVICE_FILE), "w", encoding="utf-8") as f:
        json.dump({"ip": ip, "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M")},
                  f, ensure_ascii=False)


def valid_ip(value):
    parts = str(value or "").strip().split(".")
    if len(parts) != 4:
        return None
    for p in parts:
        if not p.isdigit() or not (0 <= int(p) <= 255):
            return None
    return ".".join(str(int(p)) for p in parts)


def normalize_employee_number(value):
    """يوحّد شكل رقم الموظف للمقارنة فقط (لا يُستخدم للحفظ أبدًا): يحذف كل
    المسافات والمحارف الخفية، ويحوّل الأرقام العربية-الهندية إلى إنجليزية،
    والحروف اللاتينية لحالة كبيرة، ويحذف الأصفار البادئة من الأرقام الخالصة
    (لأن إكسل يحوّل 066 إلى 66 تلقائيًا)."""
    s = re.sub(r"\s+", "", clean_cell_text(value)).upper()
    if s.isdigit():
        s = s.lstrip("0") or "0"
    return s


def clean_cell_text(value):
    """نص الخلية بعد تحويل الأرقام العربية-الهندية وحذف المحارف الخفية."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).translate(_INVISIBLE_CHARS).translate(_ARABIC_DIGITS).strip()


def clean_employee_number(raw):
    """يرجع (الرقم كما كُتب بعد حذف المسافات الطرفية، رسالة خطأ أو None).
    رقم الموظف نص وليس عددًا - يقبل أرقامًا مثل NN-749397."""
    s = str(raw if raw is not None else "").strip()
    if not s:
        return None, "أدخل رقم الموظف"
    if re.search(r"\s", s):
        return None, "رقم الموظف يجب ألا يحتوي على مسافات"
    if len(s.encode("utf-8")) > MAX_EMPLOYEE_NUMBER_BYTES:
        return None, "رقم الموظف أطول من المسموح به في الجهاز"
    return s, None


def next_free_uid(users):
    """أول رقم داخلي بعد أكبر رقم مستخدم (نفس سلوك الجهاز ومكتبة pyzk)."""
    return max((u.uid for u in users), default=0) + 1


def index_by_employee_number(users):
    """فهرس {رقم موظف موحّد: المستخدم} لتسريع المطابقة الجماعية."""
    return {normalize_employee_number(u.user_id): u for u in users if u.user_id}


def connect_reliable(device):
    """اتصال موثوق لعمليات الموظفين (قراءة القائمة والكتابة): TCP أولًا ثم
    UDP احتياطًا.

    قائمة الموظفين وقوالب البصمات بيانات كبيرة؛ وUDP لا يضمن وصولها ولا
    يُبلّغ عن ضياع جزء منها، فتصل القائمة مبتورة بصمت (فيظهر موظف مسجّل
    كأنه غير موجود، وتختلف النتيجة بين محاولة وأخرى)، أو يصل قالب ناقص
    يُسجَّل ولا يطابق صاحبه. TCP يضمن الوصول كاملًا أو يعطي خطأ صريحًا.
    يرجع (الاتصال، اسم البروتوكول المستخدم)."""
    last_error = None
    for use_udp in (False, True):
        try:
            conn = _zk_for(device, force_udp=use_udp, timeout=IMPORT_TIMEOUT).connect()
            return conn, ("UDP" if use_udp else "TCP")
        except Exception as e:
            last_error = e
    raise last_error


def fetch_users_verified(conn, attempts=3):
    """يقرأ قائمة الموظفين ويتحقق من اكتمالها قبل الاعتماد عليها.

    الجهاز يعلن عدد موظفيه المسجّلين، فنقارنه بما وصلنا فعلًا ونعيد القراءة
    عند النقص. بدون هذا الفحص تمر القائمة المبتورة بلا أي إشارة، وتُبنى
    عليها نتائج خاطئة تبدو صحيحة.
    يرجع (القائمة، العدد المعلَن، هل هي مكتملة)."""
    users = []
    declared = None
    for attempt in range(attempts):
        users = conn.get_users()
        declared = getattr(conn, "users", None)
        try:
            declared = int(declared) if declared is not None else None
        except (TypeError, ValueError):
            declared = None
        if declared is None or len(users) >= declared:
            return users, declared, True
        if attempt + 1 < attempts:
            time.sleep(0.4)
    return users, declared, False


def _fingers_from_export(uid, exported):
    """يبني كائنات البصمات من الملف المُصدَّر (القالب محفوظ نصًا ست عشريًا)."""
    from zk.finger import Finger
    fingers = []
    for f in exported or []:
        tpl = f.get("template")
        if not tpl:
            continue
        fingers.append(Finger(uid, int(f.get("fid", 0)), int(f.get("valid", 1)), bytes.fromhex(tpl)))
    return fingers


def _templates_on_device(conn, uid):
    """{fid: bytes} للقوالب الموجودة فعليًا على الجهاز لهذا الرقم الداخلي."""
    result = {}
    for t in conn.get_templates():
        if t.uid == uid:
            result[t.fid] = bytes(t.template)
    return result


def _write_fingers_verified(conn, user_obj, fingers):
    """يكتب القوالب ثم يقرأها من الجهاز ويقارنها بايتًا ببايت.

    هذا التحقق ضروري: الكتابة قد "تنجح" من طرف التطبيق بينما لا يصل للجهاز
    شيء، أو يصل قالب ناقص يبدو مسجّلًا لكنه لا يطابق صاحبه عند البصم.
    يعيد المحاولة مرة واحدة، ويرجع (عدد المؤكَّد، رسالة الخطأ أو None)."""
    attempts = 2
    last_missing = []
    for attempt in range(attempts):
        conn.save_user_template(user_obj, fingers)
        try:
            conn.refresh_data()
        except Exception:
            pass

        on_device = _templates_on_device(conn, user_obj.uid)
        last_missing = [f.fid for f in fingers
                        if on_device.get(f.fid) != bytes(f.template)]
        if not last_missing:
            return len(fingers), None
        if attempt + 1 < attempts:
            time.sleep(0.5)

    confirmed = len(fingers) - len(last_missing)
    return confirmed, f"لم تُحفظ {len(last_missing)} بصمة على الجهاز بشكل صحيح"


def check_connectivity(ip, timeout=CONNECT_TIMEOUT):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((ip.strip(), 4370))
        s.close()
        return True
    except Exception:
        return False


def get_device_lock(ip):
    with _device_locks_guard:
        if ip not in _device_locks:
            _device_locks[ip] = threading.Lock()
        return _device_locks[ip]


def _zk_for(device, force_udp=True, timeout=None):
    return ZK(device["ip"], port=4370, timeout=timeout or ZK_TIMEOUT,
              password=int(device.get("comm_key", 0) or 0), force_udp=force_udp, ommit_ping=True)


def read_device_hardware(conn):
    """يقرأ معلومات الجهاز المهمة للتوافق. كل قراءة مستقلة داخل try لأن بعض
    الطرازات لا تدعم بعض الاستعلامات، وفشل واحدة يجب ألا يُفشل الباقي."""
    info = {}
    for key, getter in (
        ("fp_version", "get_fp_version"),
        ("device_name", "get_device_name"),
        ("platform", "get_platform"),
        ("firmware", "get_firmware_version"),
        ("serial", "get_serialnumber"),
    ):
        try:
            value = getattr(conn, getter)()
            info[key] = str(value).strip() if value is not None else None
        except Exception:
            info[key] = None
    return info


# ---------------------------------------------------------------------------
# عملية الاستيراد (نفس منطق التطبيق الأصلي، بلا خيارات)
# ---------------------------------------------------------------------------

_import_jobs = {}
_IMPORT_JOB_TTL_SECONDS = 1800


def _run_import_job(job_id, device, employees):
    from zk.user import User

    job = _import_jobs[job_id]
    try:
        with get_device_lock(device["ip"]):
            conn = None
            device_disabled = False
            try:
                conn, protocol = connect_reliable(device)
                job["protocol"] = protocol

                # أجهزة ZK قد تتجاهل الكتابة بصمت أثناء انشغالها باستقبال البصم
                try:
                    conn.disable_device()
                    device_disabled = True
                except Exception:
                    pass

                users, declared, complete = fetch_users_verified(conn)
                if not complete:
                    raise IOError(
                        f"قائمة الموظفين وصلت ناقصة ({len(users)} من {declared}) — "
                        "أُلغيت العملية تفاديًا لإنشاء موظفين مكررين")
                index = index_by_employee_number(users)
                next_uid = next_free_uid(users)

                for emp in employees:
                    shown_number = str(emp.get("user_id") or "—")
                    try:
                        number, number_err = clean_employee_number(emp.get("user_id"))
                        if number_err:
                            raise ValueError(number_err)

                        # لا كتابة فوق الموجودين - قرار مثبّت في هذا التطبيق
                        if index.get(normalize_employee_number(number)):
                            job["skipped"] += 1
                            continue

                        name = str(emp.get("name") or "")
                        privilege = int(emp.get("privilege") or 0)
                        password = str(emp.get("password") or "")
                        group_id = str(emp.get("group_id") or "")
                        card = int(emp.get("card") or 0)

                        if next_uid > MAX_DEVICE_UID:
                            raise ValueError("امتلأت سعة الجهاز من الموظفين")
                        uid = next_uid
                        next_uid += 1

                        conn.set_user(uid=uid, name=name, privilege=privilege, password=password,
                                      group_id=group_id, user_id=number, card=card)

                        fingers = _fingers_from_export(uid, emp.get("fingers"))
                        if fingers:
                            user_obj = User(uid, name, privilege, password, group_id, number, card)
                            confirmed, finger_error = _write_fingers_verified(conn, user_obj, fingers)
                            job["fingers"] += confirmed
                            job["fingers_sent"] += len(fingers)
                            if finger_error:
                                job["finger_failed"].append({"user_id": number, "reason": finger_error})

                        job["added"] += 1
                        # يمنع تكرار نفس الرقم لو ورد مرتين داخل الملف نفسه
                        index[normalize_employee_number(number)] = User(
                            uid, name, privilege, password, group_id, number, card)
                    except Exception as e:
                        job["failed"].append({"user_id": shown_number, "reason": str(e)})
                    finally:
                        job["done"] += 1

                try:
                    conn.refresh_data()
                except Exception:
                    pass
            finally:
                if conn:
                    if device_disabled:
                        try:
                            conn.enable_device()
                        except Exception:
                            pass
                    conn.disconnect()
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["message"] = str(e)


# ---------------------------------------------------------------------------
# واجهات API
# ---------------------------------------------------------------------------

def _web_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


@app.route('/')
def index():
    return send_from_directory(_web_dir(), "index.html")


@app.route('/<path:filename>')
def static_files(filename):
    return send_from_directory(_web_dir(), filename)


@app.route('/api/device', methods=['GET'])
def api_device_get():
    return jsonify({"success": True, "ip": load_device().get("ip", "")})


@app.route('/api/device/check', methods=['POST'])
def api_device_check():
    """فحص الاتصال بالجهاز وحفظ عنوانه. قراءة فقط، لا يكتب على الجهاز شيئًا."""
    ip = valid_ip((request.json or {}).get("ip"))
    if not ip:
        return jsonify({"success": False, "message": "عنوان الجهاز غير صحيح — مثال: 192.168.1.201"}), 400

    if not check_connectivity(ip):
        return jsonify({
            "success": False,
            "message": "لا يمكن الوصول إلى الجهاز — تأكد أن هاتفك على نفس شبكة الجهاز وأن العنوان صحيح",
        }), 400

    device = {"ip": ip, "name": ip, "comm_key": 0}
    try:
        with get_device_lock(ip):
            conn = None
            try:
                conn, protocol = connect_reliable(device)
                users, declared, complete = fetch_users_verified(conn)
                hardware = read_device_hardware(conn)
            finally:
                if conn:
                    conn.disconnect()
    except Exception as e:
        return jsonify({"success": False, "message": f"تعذّر الاتصال بالجهاز: {e}"}), 500

    save_device(ip)
    return jsonify({
        "success": True, "ip": ip, "protocol": protocol,
        "employees_count": len(users), "complete": complete,
        "device_name": hardware.get("device_name") or "",
    })


@app.route('/api/import/start', methods=['POST'])
def api_import_start():
    data = request.json or {}
    ip = valid_ip(data.get("ip"))
    if not ip:
        return jsonify({"success": False, "message": "افحص الاتصال بالجهاز أولًا"}), 400

    employees = data.get("employees")
    if not isinstance(employees, list) or not employees:
        return jsonify({"success": False, "message": "الملف لا يحتوي على موظفين"}), 400

    if not check_connectivity(ip):
        return jsonify({"success": False, "message": "الجهاز غير متصل — افحص الاتصال مرة أخرى"}), 400

    now = datetime.now()
    for jid in [j for j, v in _import_jobs.items()
                if (now - v["created"]).total_seconds() > _IMPORT_JOB_TTL_SECONDS]:
        _import_jobs.pop(jid, None)

    job_id = str(uuid.uuid4())
    _import_jobs[job_id] = {
        "status": "running", "total": len(employees), "done": 0,
        "added": 0, "skipped": 0, "fingers": 0, "fingers_sent": 0,
        "failed": [], "finger_failed": [], "message": "", "protocol": None,
        "created": now, "ip": ip,
    }
    threading.Thread(target=_run_import_job,
                     args=(job_id, {"ip": ip, "name": ip, "comm_key": 0}, employees),
                     daemon=True).start()
    return jsonify({"success": True, "job_id": job_id})


@app.route('/api/import/status/<job_id>', methods=['GET'])
def api_import_status(job_id):
    job = _import_jobs.get(job_id)
    if not job:
        return jsonify({"success": False, "message": "العملية غير موجودة"}), 404
    out = {k: v for k, v in job.items() if k != "created"}
    out["success"] = True
    return jsonify(out)


def start(base_dir=None):
    """نقطة الدخول التي يستدعيها تطبيق أندرويد (MainActivity.kt) عبر Chaquopy."""
    global BASE_DIR
    if base_dir:
        BASE_DIR = base_dir
    os.makedirs(BASE_DIR, exist_ok=True)
    try:
        app.run(host="127.0.0.1", port=5001, threaded=True)
    except SystemExit:
        pass
    except OSError:
        pass


if __name__ == '__main__':
    start()
