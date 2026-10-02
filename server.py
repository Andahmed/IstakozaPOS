# -*- coding: utf-8 -*-
"""
سيرفر نقطة البيع - إستاكوزا  (Istakoza POS server)
- بايثون فقط (من غير مكتبات خارجية): http.server + sqlite3
- بيقدّم الصفحة pos_istakoza.html وبيحفظ كل البيانات في قاعدة SQLite (pos_data.db)
- مكان البيانات: C:\\ProgramData\\Istakoza  (لما يتشغل كـ exe)  أو فولدر البرنامج (لما يتشغل من السورس)
  ممكن تغييره بمتغير البيئة ISTAKOZA_DATA، والبورت بـ ISTAKOZA_PORT (الافتراضي 8080)
"""
import os, sys, re, json, base64, hashlib, secrets, threading, queue, socket, ipaddress, sqlite3, webbrowser, subprocess
from contextlib import contextmanager
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

VERSION = 4
FROZEN = getattr(sys, 'frozen', False)
APP_DIR = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))


def data_dir():
    d = os.environ.get('ISTAKOZA_DATA')
    if not d and FROZEN and os.name == 'nt':
        d = os.path.join(os.environ.get('PROGRAMDATA', APP_DIR), 'Istakoza')
    d = d or APP_DIR
    os.makedirs(d, exist_ok=True)
    return d


def resource_path(rel):
    # لو شغال كـ exe: الصفحة متحشورة جوه الـ exe (PyInstaller --add-data) وبتتفك في sys._MEIPASS
    base = getattr(sys, '_MEIPASS', None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, rel)


def ui_version(path):
    try:
        m = re.search(r'name="pos-ui-version" content="(\d+)"', open(path, encoding='utf-8').read(65536))
        return int(m.group(1)) if m else 0
    except OSError:
        return 0


def find_html():
    # 1) نسخة معدّلة اختيارية في فولدر الداتا (تتجاهل لو أقدم من السيرفر عشان ماتبوّظش التوافق)
    # 2) الصفحة المتحشورة جوه الـ exe  3) ملف جنب البرنامج (للتشغيل من السورس)
    custom = os.path.join(data_dir(), 'pos_istakoza.html')
    if os.path.isfile(custom):
        if ui_version(custom) >= VERSION:
            return custom
        print('تجاهل pos_istakoza.html القديمة في فولدر الداتا (إصدارها أقدم من السيرفر):', custom)
    for p in (resource_path('pos_istakoza.html'), os.path.join(APP_DIR, 'pos_istakoza.html')):
        if os.path.isfile(p):
            return p
    return resource_path('pos_istakoza.html')


DB = os.path.join(data_dir(), 'pos_data.db')
HTML = find_html()
PORT = int(os.environ.get('ISTAKOZA_PORT', '8080'))
LOCK = threading.Lock()
TOK = {}
SUBS, SUBS_LOCK = set(), threading.Lock()


def now_s():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# ------------------------------------------------------------------ live events (SSE)
def broadcast(ev):
    with SUBS_LOCK:
        for q in list(SUBS):
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass


# ------------------------------------------------------------------ database
T = {
    'Products': 'PID INTEGER PRIMARY KEY,PName TEXT,Cat TEXT,Unit TEXT,Price REAL,Icon TEXT,Code TEXT,Hidden INTEGER DEFAULT 0',
    'Stock': 'PID INTEGER PRIMARY KEY,Qty REAL',
    'Users': 'UName TEXT PRIMARY KEY,PHash TEXT,Role TEXT,FullName TEXT',
    'Settings': 'K TEXT PRIMARY KEY,V TEXT',
    'Sales': 'SID TEXT PRIMARY KEY,SNo INTEGER,SDay TEXT,STime TEXT,SHour INTEGER,SType TEXT,Cashier TEXT,CName TEXT,CPhone TEXT,CAddr TEXT,SSub REAL,SDisc REAL,STax REAL,SFee REAL,STotal REAL,Closed INTEGER DEFAULT 0',
    'SaleItems': 'SID TEXT,PID INTEGER,IName TEXT,Cat TEXT,Unit TEXT,Qty REAL,Price REAL',
    # v2: سجل الإلغاء والتعديل
    # v3: قفل الأيام بعد التقفيل النهائي
    'day_locks': 'day TEXT PRIMARY KEY,locked_at TEXT,locked_by TEXT',
    # v4: مرتجع + طلبيات الشراء
    'Returns': 'RID TEXT PRIMARY KEY,RNo INTEGER,SID TEXT,SNo INTEGER,RDay TEXT,RTime TEXT,Reason TEXT,Cashier TEXT,Manager TEXT,Total REAL,Status TEXT,PaidBy TEXT,PaidAt TEXT,Log TEXT',
    'ReturnItems': 'RID TEXT,PID INTEGER,IName TEXT,Cat TEXT,Unit TEXT,Qty REAL,Price REAL,Refund REAL',
    'Suppliers': 'SupID INTEGER PRIMARY KEY AUTOINCREMENT,SName TEXT,Phone TEXT,Note TEXT',
    'StoreItems': 'Code TEXT PRIMARY KEY,IName TEXT,Unit TEXT,Cat TEXT,Price REAL,SupID INTEGER',
    'PoTemplates': 'TID INTEGER PRIMARY KEY AUTOINCREMENT,TName TEXT,Items TEXT',
    'POrders': 'POID TEXT PRIMARY KEY,PONo TEXT,PODay TEXT,POTime TEXT,Supplier TEXT,Total REAL,Status TEXT,CreatedBy TEXT,SentAt TEXT,Note TEXT,Err TEXT',
    'POItems': 'POID TEXT,Code TEXT,IName TEXT,Unit TEXT,Qty REAL,Price REAL',
    'SaleLog': 'LID INTEGER PRIMARY KEY AUTOINCREMENT,SID TEXT,Act TEXT,ByUser TEXT,Reason TEXT,At TEXT,Detail TEXT',
}
# v2: أعمدة جديدة في Sales (بتتضاف تلقائياً لقاعدة بيانات قديمة)
SALES_NEW = {'Void': 'INTEGER DEFAULT 0', 'VReason': 'TEXT', 'VBy': 'TEXT', 'VAt': 'TEXT',
             'EBy': 'TEXT', 'EAt': 'TEXT', 'ECount': 'INTEGER DEFAULT 0', 'Comment': 'TEXT', 'Returned': 'INTEGER DEFAULT 0'}


def hp(u, p):
    return hashlib.sha256(('istakoza|' + u.lower() + '|' + p).encode('utf-8')).hexdigest()


@contextmanager
def db():
    c = sqlite3.connect(DB, timeout=30)
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init():
    with db() as c:
        c.execute('PRAGMA journal_mode=WAL')
        for n, d in T.items():
            c.execute('CREATE TABLE IF NOT EXISTS %s (%s)' % (n, d))
        have = {r[1].lower() for r in c.execute('PRAGMA table_info(Products)')}
        if 'hidden' not in have:
            c.execute('ALTER TABLE Products ADD COLUMN Hidden INTEGER DEFAULT 0')
        if 'code' not in have:
            c.execute('ALTER TABLE Products ADD COLUMN Code TEXT')
        have = {r[1].lower() for r in c.execute('PRAGMA table_info(Sales)')}
        for col, typ in SALES_NEW.items():
            if col.lower() not in have:
                c.execute('ALTER TABLE Sales ADD COLUMN %s %s' % (col, typ))
        c.execute('CREATE INDEX IF NOT EXISTS ix_items_sid ON SaleItems(SID)')
        c.execute('CREATE INDEX IF NOT EXISTS ix_sales_day ON Sales(SDay)')
        c.execute('CREATE INDEX IF NOT EXISTS ix_log_sid ON SaleLog(SID)')
        # أيام اتقفلت قبل التحديث ده: نسجّلها في day_locks
        c.execute("INSERT OR IGNORE INTO day_locks (day,locked_at,locked_by) SELECT DISTINCT SDay,?,'migration' FROM Sales WHERE Closed=1", (now_s(),))
        if c.execute('SELECT COUNT(*) FROM Users').fetchone()[0] == 0:
            c.execute('INSERT INTO Users VALUES (?,?,?,?)', ('admin', hp('admin', 'admin'), 'admin', 'المدير'))


def get_cfg(c):
    row = c.execute("SELECT V FROM Settings WHERE K='cfg'").fetchone()
    try:
        return json.loads(row[0]) if row else {}
    except Exception:
        return {}


PERM_DEF = {'return': 1, 'reprint': 1, 'price': 1, 'del': 1, 'disc': 1}


def can(user, perm):
    """المدير يقدر على كل حاجة، والكاشير بالصلاحيات اللي المدير إداها له (cfg.perm)"""
    if user['role'] == 'admin':
        return True
    with db() as c:
        return bool((get_cfg(c).get('perm') or {}).get(perm, PERM_DEF.get(perm, 0)))


SALE_COLS = ('SID,SNo,SDay,STime,SHour,SType,Cashier,CName,CPhone,CAddr,SSub,SDisc,STax,SFee,STotal,Closed,'
             'Void,VReason,VBy,VAt,EBy,EAt,ECount,Comment,Returned')


def row_to_sale(r, items):
    return dict(id=r[0], no=r[1], day=r[2], time=r[3], h=r[4], type=r[5], by=r[6],
                sub=r[10], disc=r[11], tax=r[12], fee=r[13], total=r[14], items=items,
                cust=dict(name=r[7], phone=r[8], addr=r[9]) if r[5] == 'delivery' else None,
                void=int(r[16] or 0), vreason=r[17] or '', vby=r[18] or '', vat=r[19] or '',
                eby=r[20] or '', eat=r[21] or '', ecount=int(r[22] or 0), comment=r[23] or '', returned=int(r[24] or 0))


def get_state():
    with db() as c:
        prods = [dict(id=r[0], name=r[1], cat=r[2], unit=r[3], price=r[4], icon=r[5], code=r[6], hidden=bool(r[7]))
                 for r in c.execute('SELECT PID,PName,Cat,Unit,Price,Icon,Code,Hidden FROM Products ORDER BY PID')]
        items = {}
        for r in c.execute('SELECT SID,PID,IName,Cat,Unit,Qty,Price FROM SaleItems'):
            items.setdefault(r[0], []).append(dict(id=r[1], name=r[2], cat=r[3], unit=r[4], qty=r[5], price=r[6]))
        locked = {r[0] for r in c.execute('SELECT day FROM day_locks')}
        S, A = [], []
        for r in c.execute('SELECT %s FROM Sales ORDER BY SDay,SID' % SALE_COLS):
            sl = row_to_sale(r, items.get(r[0], []))
            sl['locked'] = r[2] in locked
            (A if r[15] else S).append(sl)
        stock = {str(r[0]): r[1] for r in c.execute('SELECT PID,Qty FROM Stock')}
        return dict(products=prods, sales=S, archive=A, stock=stock, cfg=get_cfg(c), ver=VERSION, returns=list_returns(c))


def save_state(d, role):
    with LOCK, db() as c:
        if role == 'admin':
            c.execute('DELETE FROM Products')
            c.executemany('INSERT INTO Products (PID,PName,Cat,Unit,Price,Icon,Code,Hidden) VALUES (?,?,?,?,?,?,?,?)',
                          [(int(p['id']), p['name'], p.get('cat', ''), p.get('unit', 'pc'), float(p.get('price') or 0),
                            p.get('icon', ''), str(p.get('code') or ''), 1 if p.get('hidden') else 0)
                           for p in d.get('products', [])])
            c.execute('DELETE FROM Stock')
            c.executemany('INSERT INTO Stock VALUES (?,?)', [(int(k), float(v)) for k, v in (d.get('stock') or {}).items()])
            c.execute('DELETE FROM Settings')
            c.execute('INSERT INTO Settings VALUES (?,?)', ('cfg', json.dumps(d.get('cfg') or {}, ensure_ascii=False)))
        else:  # الكاشير: تعديل سعر اليوم فقط
            for p in d.get('products', []):
                c.execute('UPDATE Products SET Price=? WHERE PID=?', (float(p.get('price') or 0), int(p['id'])))


def ins_sale(c, s, closed=0):
    cu = s.get('cust') or {}
    c.execute('INSERT INTO Sales (SID,SNo,SDay,STime,SHour,SType,Cashier,CName,CPhone,CAddr,SSub,SDisc,STax,SFee,STotal,Closed,'
              'Void,VReason,VBy,VAt,EBy,EAt,ECount,Comment,Returned) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
              (s['id'], s['no'], s['day'], s['time'], s.get('h', 0), s.get('type', 'cash'), s.get('by', ''),
               cu.get('name', ''), cu.get('phone', ''), cu.get('addr', ''), s['sub'], s['disc'], s.get('tax', 0),
               s.get('fee', 0), s['total'], closed, 1 if s.get('void') else 0, s.get('vreason', ''), s.get('vby', ''),
               s.get('vat', ''), s.get('eby', ''), s.get('eat', ''), s.get('ecount', 0), s.get('comment', ''), s.get('returned', 0)))
    put_items(c, s['id'], s['items'])


def put_items(c, sid, items):
    c.executemany('INSERT INTO SaleItems VALUES (?,?,?,?,?,?,?)',
                  [(sid, i['id'], i['name'], i.get('cat') or '', i['unit'], i['qty'], i['price']) for i in items])


def add_sale(d, user):
    # الترقيم بيتم هنا جوه السيرفر (داخل قفل) عشان جهازين مايطلعوش نفس رقم الفاتورة
    with LOCK, db() as c:
        ex = c.execute('SELECT SNo FROM Sales WHERE SID=?', (d['id'],)).fetchone()
        if ex:
            return {'ok': 1, 'no': ex[0], 'id': d['id'], 'dup': 1}
        d['no'] = (c.execute('SELECT MAX(SNo) FROM Sales WHERE Closed=0').fetchone()[0] or 0) + 1
        d['by'] = user['u']
        ins_sale(c, d)
    return {'ok': 1, 'no': d['no'], 'id': d['id']}


# ------------------------------------------------------------------ void / edit (v2) + قفل الأيام (v3)
LOCK_MSG = '🚫 غير مسموح! هذا اليوم تم تقفيله رسمياً. استخدم شاشة المرتجع بتاريخ اليوم.'


class Forbidden(Exception):
    pass


def check_unlocked(c, day):
    if c.execute('SELECT 1 FROM day_locks WHERE day=?', (day,)).fetchone():
        raise Forbidden(LOCK_MSG)
def log(c, sid, act, user, reason, detail):
    c.execute('INSERT INTO SaleLog (SID,Act,ByUser,Reason,At,Detail) VALUES (?,?,?,?,?,?)',
              (sid, act, user['u'], reason, now_s(), json.dumps(detail, ensure_ascii=False)))


def snapshot(c, sid):
    r = c.execute('SELECT %s FROM Sales WHERE SID=?' % SALE_COLS, (sid,)).fetchone()
    if not r:
        raise Exception('الأوردر غير موجود')
    items = [dict(id=i[0], name=i[1], cat=i[2], unit=i[3], qty=i[4], price=i[5])
             for i in c.execute('SELECT PID,IName,Cat,Unit,Qty,Price FROM SaleItems WHERE SID=?', (sid,))]
    return row_to_sale(r, items), bool(r[15])


def stock_back(c, old_items, new_items):
    """أوردر من يوم اتقفل: المخزن اتخصم منه وقت القفل، فلازم نرجّع الفرق (المخزن للأصناف اللي ليها رصيد بس)"""
    delta = {}
    for i in old_items:
        delta[i['id']] = delta.get(i['id'], 0) + i['qty']
    for i in new_items:
        delta[i['id']] = delta.get(i['id'], 0) - i['qty']
    for pid, q in delta.items():
        if abs(q) > 1e-9:
            c.execute('UPDATE Stock SET Qty=Qty+? WHERE PID=?', (round(q, 3), pid))


def do_void(d, user):
    reason = str(d.get('reason', '')).strip()
    if not reason:
        raise Exception('اكتب سبب الإلغاء')
    with LOCK, db() as c:
        s, closed = snapshot(c, d['id'])
        check_unlocked(c, s['day'])
        if s['returned']:
            raise Exception('الأوردر عليه مرتجع - مينفعش يتلغي')
        if s['void']:
            raise Exception('الأوردر ملغي بالفعل')
        c.execute('UPDATE Sales SET Void=1,VReason=?,VBy=?,VAt=? WHERE SID=?', (reason, user['u'], now_s(), d['id']))
        if closed:
            stock_back(c, s['items'], [])
        log(c, d['id'], 'void', user, reason, {'before': s})


def do_edit(d, user):
    reason = str(d.get('reason', '')).strip()
    if not reason:
        raise Exception('اكتب سبب التعديل')
    items = []
    for i in d.get('items') or []:
        q, p = float(i['qty']), float(i['price'])
        if q <= 0 or p < 0:
            raise Exception('كمية أو سعر غير صحيح')
        items.append(dict(id=int(i['id']), name=str(i['name']), cat=str(i.get('cat') or ''),
                          unit='kg' if i.get('unit') == 'kg' else 'pc', qty=round(q, 3), price=p))
    if not items:
        raise Exception('الأوردر لازم يفضل فيه صنف واحد على الأقل (للإلغاء استخدم Void)')
    with LOCK, db() as c:
        s, closed = snapshot(c, d['id'])
        check_unlocked(c, s['day'])
        if s['returned']:
            raise Exception('الأوردر عليه مرتجع - مينفعش يتعدل')
        if s['void']:
            raise Exception('الأوردر ملغي - مينفعش يتعدل')
        cu = d.get('cust') or {}
        if s['type'] == 'delivery':
            cn, cp, ca = cu.get('name', ''), cu.get('phone', ''), cu.get('addr', '')
        else:
            cn, cp, ca = '', '', ''
        c.execute('UPDATE Sales SET SSub=?,SDisc=?,STax=?,SFee=?,STotal=?,CName=?,CPhone=?,CAddr=?,EBy=?,EAt=?,ECount=ECount+1,Comment=? WHERE SID=?',
                  (float(d['sub']), float(d['disc']), float(d.get('tax', 0)), float(d.get('fee', 0)), float(d['total']),
                   cn, cp, ca, user['u'], now_s(), str(d.get('comment', s['comment']) or '').strip(), d['id']))
        c.execute('DELETE FROM SaleItems WHERE SID=?', (d['id'],))
        put_items(c, d['id'], items)
        if closed:
            stock_back(c, s['items'], items)
        after, _ = snapshot(c, d['id'])
        log(c, d['id'], 'edit', user, reason, {'before': s, 'after': after})


def get_log(sid):
    with db() as c:
        return [dict(act=r[0], by=r[1], reason=r[2], at=r[3])
                for r in c.execute('SELECT Act,ByUser,Reason,At FROM SaleLog WHERE SID=? ORDER BY LID', (sid,))]


# ------------------------------------------------------------------ misc
def do_print(d):
    """يبعت بيانات ESC/POS خام للطابعة على الشبكة (بورت 9100)"""
    ip = str(d.get('ip', '')).strip()
    port = int(d.get('port') or 9100)
    try:
        a = ipaddress.ip_address(ip)
    except Exception:
        raise Exception('عنوان IP غير صحيح')
    if not (a.is_private or a.is_loopback):
        raise Exception('الـ IP لازم يكون على الشبكة المحلية')
    try:
        data = base64.b64decode(d.get('data', ''))
    except Exception:
        raise Exception('بيانات الطباعة غير صالحة')
    try:
        with socket.create_connection((ip, port), timeout=5) as s:
            s.settimeout(10)
            s.sendall(data)
    except Exception as e:
        raise Exception('تعذر الاتصال بالطابعة %s:%d (%s)' % (ip, port, e))


def do_import(d):
    save_state(d, 'admin')
    with LOCK, db() as c:
        c.execute('DELETE FROM Sales')
        c.execute('DELETE FROM SaleItems')
        for closed, lst in ((1, d.get('archive', [])), (0, d.get('sales', []))):
            for k, s in enumerate(lst):
                s['id'] = s.get('id') or '%s-%s-%s-%d' % (s['day'], s['no'], closed, k)
                ins_sale(c, s, closed)
        c.execute('DELETE FROM day_locks')
        c.execute("INSERT OR IGNORE INTO day_locks (day,locked_at,locked_by) SELECT DISTINCT SDay,?,'import' FROM Sales WHERE Closed=1", (now_s(),))


def reset():
    with LOCK, db() as c:
        for t in ('Sales', 'SaleItems', 'Stock', 'SaleLog', 'day_locks', 'Returns', 'ReturnItems'):
            c.execute('DELETE FROM ' + t)


def login(u, p):
    with db() as c:
        r = c.execute('SELECT UName,Role,FullName FROM Users WHERE UName=? AND PHash=?', (u, hp(u, p))).fetchone()
    return {'u': r[0], 'role': r[1], 'full': r[2]} if r else None


def list_users():
    with db() as c:
        return [dict(u=r[0], role=r[1], full=r[2]) for r in c.execute('SELECT UName,Role,FullName FROM Users')]


def save_user(d):
    u = d['u'].strip()
    if not re.fullmatch(r'[\w.-]+', u):
        raise Exception('اسم الدخول: حروف وأرقام بدون مسافات')
    with LOCK, db() as c:
        ex = c.execute('SELECT Role FROM Users WHERE UName=?', (u,)).fetchone()
        if d.get('del'):
            if ex and ex[0] == 'admin' and c.execute("SELECT COUNT(*) FROM Users WHERE Role='admin'").fetchone()[0] <= 1:
                raise Exception('لا يمكن حذف آخر مدير')
            c.execute('DELETE FROM Users WHERE UName=?', (u,))
        elif ex:
            if d.get('p'):
                c.execute('UPDATE Users SET PHash=? WHERE UName=?', (hp(u, d['p']), u))
        else:
            if not d.get('p'):
                raise Exception('أدخل كلمة السر')
            c.execute('INSERT INTO Users VALUES (?,?,?,?)', (u, hp(u, d['p']), d.get('role', 'cashier'), d.get('full', '')))


# ------------------------------------------------------------------ مرتجع (v4) - 4 أقفال
# 1) بيختار فاتورة حقيقية من السيستم والأصناف بتيجي منها  2) كل صنف يرجع مرة واحدة بس (بالكمية)
# 3) باسورد مدير + سبب من قايمة مقفولة  4) فلوس المرتجع "مديونية مرتجع" مش من الدرج: المدير يصرفها من خزنة منفصلة
RET_REASONS_DEF = ['أوردر غلط', 'جودة سيئة', 'الزبون لغى', 'تأخير في التوصيل', 'صنف ناقص', 'أخرى']
FAILS = {}


def ret_reasons(cfg):
    r = [x.strip() for x in re.split(r'[,،]', str(cfg.get('retReasons') or '')) if x.strip()]
    return r or RET_REASONS_DEF


def find_manager(c, pw):
    for u, full, h in c.execute("SELECT UName,FullName,PHash FROM Users WHERE Role='admin'").fetchall():
        if hp(u, pw) == h:
            return u, (full or u)
    return None


def full_name(c, u):
    r = c.execute('SELECT FullName FROM Users WHERE UName=?', (u,)).fetchone()
    return r[0] if r and r[0] else u


def ret_dict(c, r):
    items = [dict(id=i[0], name=i[1], cat=i[2], unit=i[3], qty=i[4], price=i[5], refund=i[6])
             for i in c.execute('SELECT PID,IName,Cat,Unit,Qty,Price,Refund FROM ReturnItems WHERE RID=?', (r[0],))]
    return dict(id=r[0], no=r[1], sid=r[2], sno=r[3], day=r[4], time=r[5], reason=r[6], by=r[7], mgr=r[8],
                total=r[9], status=r[10], paidBy=r[11] or '', paidAt=r[12] or '', log=r[13] or '', items=items)


RET_COLS = 'RID,RNo,SID,SNo,RDay,RTime,Reason,Cashier,Manager,Total,Status,PaidBy,PaidAt,Log'


def list_returns(c):
    return [ret_dict(c, r) for r in c.execute('SELECT %s FROM Returns ORDER BY RNo' % RET_COLS).fetchall()]


def do_return(d, user):
    now = datetime.now()
    key = user['u']
    fl = [t for t in FAILS.get(key, []) if (now - t).total_seconds() < 600]
    if len(fl) >= 5:
        raise Forbidden('تم إيقاف المرتجع مؤقتاً بسبب باسورد مدير غلط كذا مرة - استنى 10 دقايق')
    with LOCK, db() as c:
        cfg = get_cfg(c)
        reason = str(d.get('reason', '')).strip()
        if reason not in ret_reasons(cfg):
            raise Exception('اختار سبب المرتجع من القايمة')
        mgr = find_manager(c, str(d.get('pw', '')))
        if not mgr:
            fl.append(now)
            FAILS[key] = fl
            c.commit()
            raise Forbidden('باسورد المدير غير صحيح')
        FAILS.pop(key, None)
        s, closed = snapshot(c, d['sid'])
        if s['void']:
            raise Exception('الأوردر ملغي - مينفعش يتعمله مرتجع')
        orig, wsum = {}, {}
        for i in s['items']:
            orig[i['id']] = orig.get(i['id'], 0) + i['qty']
            wsum[i['id']] = wsum.get(i['id'], 0) + i['qty'] * i['price']
        meta = {i['id']: i for i in s['items']}
        done = {r[0]: r[1] for r in c.execute(
            'SELECT ri.PID,SUM(ri.Qty) FROM ReturnItems ri JOIN Returns r ON r.RID=ri.RID WHERE r.SID=? GROUP BY ri.PID', (s['id'],))}
        factor = (s['total'] - s['fee']) / s['sub'] if s['sub'] > 0 else 1.0
        lines, total = [], 0.0
        for it in d.get('items') or []:
            pid, q = int(it['id']), round(float(it['qty']), 3)
            if q <= 0:
                continue
            if pid not in orig:
                raise Forbidden('الصنف ده مش موجود في الفاتورة الأصلية')
            if done.get(pid, 0) + q > orig[pid] + 1e-9:
                raise Forbidden('الكمية دي رجعت قبل كده')
            price = wsum[pid] / orig[pid]
            refund = round(q * price * factor, 2)
            m = meta[pid]
            lines.append((pid, m['name'], m.get('cat') or '', m['unit'], q, round(price, 4), refund))
            total += refund
            done[pid] = done.get(pid, 0) + q
        if not lines:
            raise Exception('اختار كمية للمرتجع')
        rno = (c.execute('SELECT MAX(RNo) FROM Returns').fetchone()[0] or 0) + 1
        rid = secrets.token_hex(8)
        cname = full_name(c, user['u'])
        text = 'مرتجع فاتورة %s بواسطة كاشير %s وافق عليه مدير %s الساعة %s' % (s['no'], cname, mgr[1], now.strftime('%H:%M'))
        c.execute('INSERT INTO Returns (%s) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)' % RET_COLS,
                  (rid, rno, s['id'], s['no'], now.strftime('%Y-%m-%d'), now.strftime('%H:%M'), reason, cname, mgr[1],
                   round(total, 2), 'due', '', '', text))
        c.executemany('INSERT INTO ReturnItems VALUES (?,?,?,?,?,?,?,?)', [(rid,) + l for l in lines])
        full = all(done.get(pid, 0) >= orig[pid] - 1e-9 for pid in orig)
        c.execute('UPDATE Sales SET Returned=? WHERE SID=?', (2 if full else 1, s['id']))
        log(c, s['id'], 'return', user, text, {'rid': rid, 'total': round(total, 2), 'reason': reason})
        r = c.execute('SELECT %s FROM Returns WHERE RID=?' % RET_COLS, (rid,)).fetchone()
        return ret_dict(c, r)


def do_return_pay(d, user):
    with LOCK, db() as c:
        c.execute("UPDATE Returns SET Status='paid',PaidBy=?,PaidAt=? WHERE RID=? AND Status='due'",
                  (full_name(c, user['u']), now_s(), d['rid']))


# ------------------------------------------------------------------ طلبيات الشراء + الموردين + الأكواد المخزنية (v4)
def po_dict(c, r):
    items = [dict(code=i[0], name=i[1], unit=i[2], qty=i[3], price=i[4])
             for i in c.execute('SELECT Code,IName,Unit,Qty,Price FROM POItems WHERE POID=?', (r[0],))]
    return dict(id=r[0], no=r[1], day=r[2], time=r[3], supplier=r[4], total=r[5], status=r[6], by=r[7],
                sentAt=r[8] or '', note=r[9] or '', err=r[10] or '', items=items)


PO_COLS = 'POID,PONo,PODay,POTime,Supplier,Total,Status,CreatedBy,SentAt,Note,Err'


def get_store():
    with db() as c:
        return dict(
            suppliers=[dict(id=r[0], name=r[1], phone=r[2] or '', note=r[3] or '')
                       for r in c.execute('SELECT SupID,SName,Phone,Note FROM Suppliers ORDER BY SName')],
            items=[dict(code=r[0], name=r[1], unit=r[2], cat=r[3] or '', price=r[4] or 0, supid=r[5] or 0)
                   for r in c.execute('SELECT Code,IName,Unit,Cat,Price,SupID FROM StoreItems ORDER BY Code')],
            templates=[dict(id=r[0], name=r[1], items=json.loads(r[2] or '[]'))
                       for r in c.execute('SELECT TID,TName,Items FROM PoTemplates ORDER BY TName')],
            orders=[po_dict(c, r) for r in c.execute('SELECT %s FROM POrders ORDER BY PONo DESC LIMIT 300' % PO_COLS)])


def do_po(d, user):
    sup = str(d.get('supplier', '')).strip()
    if not sup:
        raise Exception('اكتب اسم المورد')
    with LOCK, db() as c:
        ex = c.execute('SELECT %s FROM POrders WHERE POID=?' % PO_COLS, (d['id'],)).fetchone()
        if ex:
            return po_dict(c, ex)
        lines, total = [], 0.0
        for it in d.get('items') or []:
            q = round(float(it['qty']), 3)
            row = c.execute('SELECT Code,IName,Unit,Price FROM StoreItems WHERE Code=?', (str(it['code']).strip(),)).fetchone()
            if not row:
                raise Exception('الكود %s غير موجود' % it['code'])
            if q <= 0:
                raise Exception('كمية غير صحيحة للكود %s' % it['code'])
            lines.append((d['id'], row[0], row[1], row[2], q, row[3] or 0))
            total += q * (row[3] or 0)
        if not lines:
            raise Exception('الطلبية فاضية')
        if not c.execute('SELECT 1 FROM Suppliers WHERE SName=?', (sup,)).fetchone():
            c.execute('INSERT INTO Suppliers (SName,Phone,Note) VALUES (?,?,?)', (sup, '', ''))
        n = c.execute("SELECT MAX(CAST(SUBSTR(PONo,4) AS INTEGER)) FROM POrders").fetchone()[0] or 0
        now = datetime.now()
        c.execute('INSERT INTO POrders (%s) VALUES (?,?,?,?,?,?,?,?,?,?,?)' % PO_COLS,
                  (d['id'], 'PO-%04d' % (n + 1), now.strftime('%Y-%m-%d'), now.strftime('%H:%M'), sup, round(total, 2),
                   'saved', user['u'], '', str(d.get('note', '')).strip(), ''))
        c.executemany('INSERT INTO POItems VALUES (?,?,?,?,?,?)', lines)
        return po_dict(c, c.execute('SELECT %s FROM POrders WHERE POID=?' % PO_COLS, (d['id'],)).fetchone())


def po_mark(poid, status, err=''):
    with LOCK, db() as c:
        c.execute('UPDATE POrders SET Status=?,Err=?,SentAt=? WHERE POID=?',
                  (status, err[:200], now_s() if status == 'sent' else '', poid))


def do_po_send(d):
    """إرسال الطلبية بـ HTTP للعنوان اللي في الإعدادات (poIp/poPort/poPath). لو فشل: بتفضل 'في الانتظار' وتتبعت بعدين"""
    import urllib.request
    with db() as c:
        cfg = get_cfg(c)
        r = c.execute('SELECT %s FROM POrders WHERE POID=?' % PO_COLS, (d['id'],)).fetchone()
        o = po_dict(c, r) if r else None
    if not o:
        raise Exception('الطلبية غير موجودة')
    ip = str(cfg.get('poIp') or '').strip()
    if cfg.get('poMode') != 'http' or not ip:
        raise Exception('إرسال الطلبيات HTTP غير مفعّل في الإعدادات')
    path = str(cfg.get('poPath') or '/')
    url = ip if ip.startswith('http') else 'http://%s:%d%s' % (ip, int(cfg.get('poPort') or 80), path if path.startswith('/') else '/' + path)
    body = json.dumps({'type': 'purchase_order', 'restaurant': cfg.get('name', ''), 'code': o['no'], 'date': o['day'],
                       'time': o['time'], 'supplier': o['supplier'], 'total': o['total'], 'note': o['note'],
                       'items': [dict(i, total=round(i['qty'] * i['price'], 2)) for i in o['items']]}, ensure_ascii=False).encode('utf-8')
    try:
        req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json; charset=utf-8'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            if not 200 <= resp.status < 300:
                raise Exception('HTTP %s' % resp.status)
        po_mark(d['id'], 'sent')
        return {'ok': 1, 'status': 'sent'}
    except Exception as e:
        po_mark(d['id'], 'pending', str(e))
        return {'ok': 1, 'status': 'pending', 'err': str(e)[:200]}


def save_supplier(d):
    with LOCK, db() as c:
        if d.get('del'):
            c.execute('DELETE FROM Suppliers WHERE SupID=?', (int(d['id']),))
        elif d.get('id'):
            c.execute('UPDATE Suppliers SET SName=?,Phone=?,Note=? WHERE SupID=?', (d['name'], d.get('phone', ''), d.get('note', ''), int(d['id'])))
        else:
            if not str(d.get('name', '')).strip():
                raise Exception('اكتب اسم المورد')
            c.execute('INSERT INTO Suppliers (SName,Phone,Note) VALUES (?,?,?)', (d['name'].strip(), d.get('phone', ''), d.get('note', '')))


def save_store_item(d):
    code, old = str(d.get('code', '')).strip(), str(d.get('old_code') or d.get('code', '')).strip()
    with LOCK, db() as c:
        if d.get('del'):
            c.execute('DELETE FROM StoreItems WHERE Code=?', (old,))
            return
        if not code or not str(d.get('name', '')).strip():
            raise Exception('الكود والاسم مطلوبين')
        if old != code and c.execute('SELECT 1 FROM StoreItems WHERE Code=?', (code,)).fetchone():
            raise Exception('الكود مستخدم لصنف تاني')
        vals = (code, d['name'].strip(), d.get('unit', 'kg'), d.get('cat', ''), float(d.get('price') or 0), int(d.get('supid') or 0))
        if c.execute('SELECT 1 FROM StoreItems WHERE Code=?', (old,)).fetchone():
            c.execute('UPDATE StoreItems SET Code=?,IName=?,Unit=?,Cat=?,Price=?,SupID=? WHERE Code=?', vals + (old,))
        else:
            c.execute('INSERT INTO StoreItems VALUES (?,?,?,?,?,?)', vals)


def save_template(d):
    with LOCK, db() as c:
        if d.get('del'):
            c.execute('DELETE FROM PoTemplates WHERE TID=?', (int(d['id']),))
            return
        name = str(d.get('name', '')).strip()
        if not name or not d.get('items'):
            raise Exception('اكتب اسم القالب وضيف أصناف')
        items = json.dumps([{'code': str(i['code']), 'qty': float(i['qty'])} for i in d['items']], ensure_ascii=False)
        if d.get('id'):
            c.execute('UPDATE PoTemplates SET TName=?,Items=? WHERE TID=?', (name, items, int(d['id'])))
        else:
            c.execute('INSERT INTO PoTemplates (TName,Items) VALUES (?,?)', (name, items))


# ------------------------------------------------------------------ backups (v3) - يدوي بس، من غير مسح أوتوماتيكي
BK_DIR = os.path.join(os.path.dirname(DB), 'backups')
BK_RE = re.compile(r'^pos_backup_(\d{8})_(\d{6})\.db$')


def make_backup():
    os.makedirs(BK_DIR, exist_ok=True)
    name = 'pos_backup_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '.db'
    dst = os.path.join(BK_DIR, name)
    src, out = sqlite3.connect(DB, timeout=30), sqlite3.connect(dst)
    try:
        src.backup(out)  # نسخة متسقة حتى لو في بيع شغال
    finally:
        out.close()
        src.close()
    return {'name': name, 'size': os.path.getsize(dst)}


def bk_time(name):
    m = BK_RE.match(name)
    return datetime.strptime(m.group(1) + m.group(2), '%Y%m%d%H%M%S') if m else None


def list_backups():
    if not os.path.isdir(BK_DIR):
        return []
    out = []
    for n in os.listdir(BK_DIR):
        t = bk_time(n)
        if t:
            out.append({'name': n, 'size': os.path.getsize(os.path.join(BK_DIR, n)), 'at': t.strftime('%Y-%m-%d %H:%M:%S')})
    return sorted(out, key=lambda x: x['name'], reverse=True)


def clean_backups(days):
    # بيمسح بس النسخ الأقدم من المدة اللي اتحددت (قرار يدوي)
    days = int(days)
    if days < 1:
        raise Exception('مدة غير صحيحة')
    cut, n = datetime.now() - timedelta(days=days), 0
    for f in list_backups():
        if bk_time(f['name']) < cut:
            os.remove(os.path.join(BK_DIR, f['name']))
            n += 1
    return n


# ------------------------------------------------------------------ http
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def out(self, code, obj, ct='application/json'):
        b = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ct + '; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(b)

    def user(self):
        return TOK.get(self.headers.get('X-Token', ''))

    def events(self):
        # بث لحظي (Server-Sent Events): كل جهاز بيسمع لأي أوردر/تعديل بيحصل من جهاز تاني
        q = parse_qs(urlparse(self.path).query)
        if not TOK.get((q.get('t') or [''])[0]):
            return self.out(401, {'error': 'login'})
        sub = queue.Queue(maxsize=500)
        with SUBS_LOCK:
            SUBS.add(sub)
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Accel-Buffering', 'no')
            self.end_headers()
            self.wfile.write(b'retry: 2000\n\n')
            self.wfile.flush()
            while True:
                try:
                    ev = sub.get(timeout=15)
                    self.wfile.write(('data: ' + json.dumps(ev, ensure_ascii=False) + '\n\n').encode('utf-8'))
                except queue.Empty:
                    self.wfile.write(b': ping\n\n')
                self.wfile.flush()
        except OSError:
            pass
        finally:
            with SUBS_LOCK:
                SUBS.discard(sub)

    def download(self):
        q = parse_qs(urlparse(self.path).query)
        u = TOK.get((q.get('t') or [''])[0])
        name = (q.get('name') or [''])[0]
        if not u or u['role'] != 'admin' or not BK_RE.match(name) or not os.path.isfile(os.path.join(BK_DIR, name)):
            return self.out(403, {'error': 'forbidden'})
        b = open(os.path.join(BK_DIR, name), 'rb').read()
        self.send_response(200)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Disposition', 'attachment; filename="%s"' % name)
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = urlparse(self.path).path
        if p in ('/', '/index.html'):
            try:
                return self.out(200, open(HTML, 'rb').read(), 'text/html')
            except OSError:
                return self.out(500, {'error': 'pos_istakoza.html not found'})
        if p == '/api/events':
            return self.events()
        if p == '/api/backup/download':
            return self.download()
        u = self.user()
        if not u:
            return self.out(401, {'error': 'login'})
        try:
            if p == '/api/state':
                return self.out(200, dict(get_state(), me=u))
            if p == '/api/users' and u['role'] == 'admin':
                return self.out(200, list_users())
            if p == '/api/store' and can(u, 'po'):
                return self.out(200, get_store())
            if p == '/api/backups' and u['role'] == 'admin':
                return self.out(200, {'dir': BK_DIR, 'files': list_backups()})
            if p == '/api/log':
                return self.out(200, get_log((parse_qs(urlparse(self.path).query).get('sid') or [''])[0]))
            self.out(403, {'error': 'forbidden'})
        except Exception as e:
            self.out(500, {'error': str(e)})

    def do_POST(self):
        try:
            d = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
        except Exception:
            d = {}
        p = self.path
        try:
            if p == '/api/login':
                u = login(d.get('u', ''), d.get('p', ''))
                if not u:
                    return self.out(400, {'error': 'اسم المستخدم أو كلمة السر غير صحيحة'})
                t = secrets.token_hex(16)
                TOK[t] = u
                return self.out(200, dict(u, token=t))
            u = self.user()
            if not u:
                return self.out(401, {'error': 'login'})
            adm = u['role'] == 'admin'
            cid = self.headers.get('X-Cid', '')
            if p == '/api/logout':
                TOK.pop(self.headers.get('X-Token'), None)
            elif p == '/api/state':
                save_state(d, u['role'])
                broadcast({'t': 'reload', 'cid': cid})
            elif p == '/api/sale':
                r = add_sale(d, u)
                if not r.get('dup'):
                    broadcast({'t': 'sale', 'sale': d, 'who': u['full'] or u['u'], 'cid': cid})
                return self.out(200, r)
            elif p == '/api/close':
                with LOCK, db() as c:
                    for i in d['ids']:
                        r = c.execute('SELECT SDay FROM Sales WHERE SID=?', (i,)).fetchone()
                        c.execute('UPDATE Sales SET Closed=1 WHERE SID=?', (i,))
                        if r:  # تسجيل اليوم في day_locks
                            c.execute('INSERT OR IGNORE INTO day_locks (day,locked_at,locked_by) VALUES (?,?,?)', (r[0], now_s(), u['u']))
                broadcast({'t': 'reload', 'cid': cid})
            elif p == '/api/void' and can(u, 'void'):
                do_void(d, u)
                broadcast({'t': 'reload', 'cid': cid})
            elif p == '/api/edit' and can(u, 'edit'):
                do_edit(d, u)
                broadcast({'t': 'reload', 'cid': cid})
            elif p == '/api/return' and can(u, 'return'):
                r = do_return(d, u)
                broadcast({'t': 'reload', 'cid': cid})
                return self.out(200, {'ok': 1, 'ret': r})
            elif adm and p == '/api/return/pay':
                do_return_pay(d, u)
                broadcast({'t': 'reload', 'cid': cid})
            elif p == '/api/po' and can(u, 'po'):
                return self.out(200, {'ok': 1, 'order': do_po(d, u)})
            elif p == '/api/po/send' and can(u, 'po'):
                return self.out(200, do_po_send(d))
            elif p == '/api/po/mark' and can(u, 'po'):
                po_mark(d['id'], 'sent' if d.get('status') == 'sent' else 'pending', str(d.get('err', '')))
            elif adm and p == '/api/po/del':
                with LOCK, db() as c:
                    c.execute('DELETE FROM POItems WHERE POID=?', (d['id'],))
                    c.execute('DELETE FROM POrders WHERE POID=?', (d['id'],))
            elif adm and p == '/api/store/supplier':
                save_supplier(d)
            elif adm and p == '/api/store/item':
                save_store_item(d)
            elif p == '/api/store/template' and can(u, 'po'):
                save_template(d)
            elif p == '/api/print':
                do_print(d)
            elif adm and p == '/api/users':
                save_user(d)
            elif adm and p == '/api/backup':
                return self.out(200, dict(make_backup(), ok=1))
            elif adm and p == '/api/backup/clean':
                return self.out(200, {'ok': 1, 'deleted': clean_backups(d.get('days', 30))})
            elif adm and p == '/api/reset':
                reset()
                broadcast({'t': 'reload', 'cid': cid})
            elif adm and p == '/api/import':
                do_import(d)
                broadcast({'t': 'reload', 'cid': cid})
            else:
                return self.out(403, {'error': 'forbidden'})
            self.out(200, {'ok': 1})
        except Forbidden as e:
            self.out(403, {'error': str(e)})
        except Exception as e:
            print('POST ERROR:', repr(e))
            self.out(400, {'error': str(e)})


def open_pos(url):
    # بيفتح البرنامج كنافذة تطبيق بطباعة صامتة (--kiosk-printing) لو Chrome/Edge موجود، وإلا المتصفح العادي
    if os.name == 'nt':
        pf = [os.environ.get(k, '') for k in ('ProgramFiles', 'ProgramFiles(x86)', 'LocalAppData')]
        cands = [os.path.join(pf[0], 'Google', 'Chrome', 'Application', 'chrome.exe'),
                 os.path.join(pf[1], 'Google', 'Chrome', 'Application', 'chrome.exe'),
                 os.path.join(pf[2], 'Google', 'Chrome', 'Application', 'chrome.exe'),
                 os.path.join(pf[1], 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
                 os.path.join(pf[0], 'Microsoft', 'Edge', 'Application', 'msedge.exe')]
        prof = os.path.join(os.environ.get('LOCALAPPDATA', APP_DIR), 'IstakozaPOS_Browser')
        for b in cands:
            if os.path.isfile(b):
                try:
                    subprocess.Popen([b, '--kiosk-printing', '--user-data-dir=' + prof, '--app=' + url])
                    return
                except OSError:
                    pass
    webbrowser.open(url)


class Srv(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def port_busy():
    try:
        with socket.create_connection(('127.0.0.1', PORT), timeout=1):
            return True
    except OSError:
        return False


if __name__ == '__main__':
    if os.name == 'nt':
        os.system('chcp 65001 >nul')
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    url = 'http://localhost:%d' % PORT
    if port_busy():
        print('البرنامج شغال بالفعل (البورت %d مستخدم) - هفتحه في المتصفح' % PORT)
        if '--no-browser' not in sys.argv:
            open_pos(url)
        sys.exit(0)
    init()
    print('السيرفر شغال: %s   (من جهاز تاني: http://IP-الجهاز:%d)' % (url, PORT))
    print('مكان البيانات:', DB)
    print('الصفحة:', HTML)
    print('الدخول الأول: admin / admin  - غيّر كلمة السر من الإعدادات')
    print('متقفلش الشاشة السودا دي طول ما بتشتغل.')
    if '--no-browser' not in sys.argv:
        threading.Timer(1.2, lambda: open_pos(url)).start()
    Srv(('0.0.0.0', PORT), H).serve_forever()
