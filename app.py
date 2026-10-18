#!/usr/bin/env python3
"""RadeGram single-file Python API + SQLite backend.
Run locally with: python app.py
For public deployment, put behind an HTTPS reverse proxy and use persistent storage.
"""
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote
from email.parser import BytesParser
from email.policy import default
from http.cookies import SimpleCookie
import hashlib, hmac, json, mimetypes, os, re, secrets, sqlite3, time, sys, webbrowser, threading, io

# Optional durable cloud services. Locally, RadeGram continues to use SQLite/files.
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
CLOUDINARY_CLOUD_NAME = os.environ.get('CLOUDINARY_CLOUD_NAME', '').strip()
CLOUDINARY_API_KEY = os.environ.get('CLOUDINARY_API_KEY', '').strip()
CLOUDINARY_API_SECRET = os.environ.get('CLOUDINARY_API_SECRET', '').strip()
CLOUD_MEDIA_ENABLED = all((CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET))
try:
    import psycopg
    from psycopg.rows import dict_row
    PG_INTEGRITY_ERRORS = (psycopg.IntegrityError,)
except ImportError:
    psycopg = None
    dict_row = None
    PG_INTEGRITY_ERRORS = ()
try:
    import cloudinary
    import cloudinary.uploader
except ImportError:
    cloudinary = None
INTEGRITY_ERRORS = (sqlite3.IntegrityError,) + PG_INTEGRITY_ERRORS

# Frozen Windows builds keep user data beside RadeGram.exe while bundled assets
# are read from PyInstaller's resource directory. Normal source runs use this folder.
if getattr(sys, 'frozen', False):
    ROOT = Path(sys.executable).resolve().parent
    RESOURCE_DIR = Path(getattr(sys, '_MEIPASS', ROOT)).resolve()
else:
    ROOT = Path(__file__).resolve().parent
    RESOURCE_DIR = ROOT
UPLOAD_DIR = Path(os.environ.get('RADEGRAM_UPLOAD_DIR', str(ROOT / 'uploads'))).resolve()
DB_PATH = Path(os.environ.get("VYRO_DB_PATH", str(ROOT / "vyro.sqlite3"))).resolve()
HOST = os.environ.get("VYRO_HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", os.environ.get("VYRO_PORT", "8000")))
MAX_UPLOAD = int(os.environ.get("VYRO_MAX_UPLOAD_MB", "1024")) * 1024 * 1024
SESSION_DAYS = 14
COOKIE_NAME = "vyro_session"
SECURE_COOKIE = os.environ.get("VYRO_SECURE_COOKIE", "0") == "1"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


class PGCursor:
    def __init__(self, cursor, lastrowid=None):
        self._cursor = cursor
        self.lastrowid = lastrowid
    def fetchone(self): return self._cursor.fetchone()
    def fetchall(self): return self._cursor.fetchall()
    @property
    def rowcount(self): return self._cursor.rowcount


class PGConnection:
    """Small adapter so the existing SQLite-style queries can use PostgreSQL."""
    _id_tables = {'users','posts','stories','comments','messages','profile_views','stickers','activities'}
    def __init__(self, connection): self._connection = connection
    def execute(self, sql, params=()):
        query = sql.replace('?', '%s')
        match = re.match(r'\s*INSERT\s+INTO\s+([a-z_]+)\s*\(', query, re.I)
        needs_id = bool(match and match.group(1).lower() in self._id_tables and ' RETURNING ' not in query.upper())
        if needs_id: query = query.rstrip().rstrip(';') + ' RETURNING id'
        cur = self._connection.execute(query, params)
        lastrowid = None
        if needs_id:
            row = cur.fetchone()
            lastrowid = row['id'] if row else None
        return PGCursor(cur, lastrowid)
    def executescript(self, script):
        for statement in script.split(';'):
            if statement.strip(): self.execute(statement)
    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb):
        try:
            self._connection.rollback() if exc_type else self._connection.commit()
        finally:
            self._connection.close()
        return False


def db():
    if DATABASE_URL:
        if psycopg is None: raise RuntimeError('PostgreSQL için psycopg bağımlılığı kurulu değil.')
        return PGConnection(psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10))
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    return con


def media_url(path):
    if not path: return ''
    value = str(path)
    return value if value.startswith(('https://', 'http://')) else '/' + value.lstrip('/')


def save_upload(data, filename, content_type='application/octet-stream'):
    if CLOUD_MEDIA_ENABLED:
        if cloudinary is None: raise RuntimeError('Cloudinary paketi kurulu değil.')
        cloudinary.config(cloud_name=CLOUDINARY_CLOUD_NAME, api_key=CLOUDINARY_API_KEY, api_secret=CLOUDINARY_API_SECRET, secure=True)
        result = cloudinary.uploader.upload(io.BytesIO(data), folder='radegram', public_id=Path(filename).stem, resource_type='auto', overwrite=False, filename_override=filename)
        return result.get('secure_url') or result['url']
    # Never silently claim permanence on Render when cloud storage is not configured.
    if os.environ.get('RENDER') and os.environ.get('RENDER') == 'true':
        raise RuntimeError('Kalıcı medya deposu ayarlanmamış. Render ayarlarına Cloudinary anahtarlarını ekle.')
    dest = UPLOAD_DIR / filename
    with dest.open('xb') as fp: fp.write(data)
    return 'uploads/' + filename


def init_postgres():
    schema = '''
    CREATE TABLE IF NOT EXISTS users(
      id BIGSERIAL PRIMARY KEY, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, display_name TEXT NOT NULL,
      username TEXT NOT NULL UNIQUE, bio TEXT NOT NULL DEFAULT '', interests TEXT NOT NULL DEFAULT '[]',
      accent TEXT NOT NULL DEFAULT '#ff3038', profile_bg TEXT NOT NULL DEFAULT 'midnight', avatar_path TEXT NOT NULL DEFAULT '', bg_image_path TEXT NOT NULL DEFAULT '', created_at BIGINT NOT NULL);
    CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, expires_at BIGINT NOT NULL);
    CREATE TABLE IF NOT EXISTS posts(
      id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, media_path TEXT NOT NULL, media_type TEXT NOT NULL,
      caption TEXT NOT NULL DEFAULT '', sticker_text TEXT NOT NULL DEFAULT '', sticker_date INTEGER NOT NULL DEFAULT 0, audio_name TEXT NOT NULL DEFAULT '', audio_artist TEXT NOT NULL DEFAULT '', audio_source TEXT NOT NULL DEFAULT '', audio_path TEXT NOT NULL DEFAULT '', created_at BIGINT NOT NULL);
    CREATE TABLE IF NOT EXISTS likes(user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE, created_at BIGINT NOT NULL, PRIMARY KEY(user_id, post_id));
    CREATE TABLE IF NOT EXISTS saves(user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE, created_at BIGINT NOT NULL, PRIMARY KEY(user_id, post_id));
    CREATE TABLE IF NOT EXISTS follows(follower_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, followed_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, created_at BIGINT NOT NULL, PRIMARY KEY(follower_id, followed_id), CHECK(follower_id <> followed_id));
    CREATE TABLE IF NOT EXISTS comments(id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE, body TEXT NOT NULL, parent_id BIGINT REFERENCES comments(id) ON DELETE CASCADE, created_at BIGINT NOT NULL);
    CREATE TABLE IF NOT EXISTS stories(id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, media_path TEXT NOT NULL, media_type TEXT NOT NULL, created_at BIGINT NOT NULL, expires_at BIGINT NOT NULL);
    CREATE TABLE IF NOT EXISTS messages(id BIGSERIAL PRIMARY KEY, sender_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, receiver_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, body TEXT NOT NULL, created_at BIGINT NOT NULL);
    CREATE TABLE IF NOT EXISTS reposts(user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE, created_at BIGINT NOT NULL, PRIMARY KEY(user_id, post_id));
    CREATE TABLE IF NOT EXISTS profile_views(id BIGSERIAL PRIMARY KEY, profile_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, viewer_id BIGINT REFERENCES users(id) ON DELETE CASCADE, viewed_at BIGINT NOT NULL);
    CREATE TABLE IF NOT EXISTS stickers(id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, label TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'text', created_at BIGINT NOT NULL);
    CREATE TABLE IF NOT EXISTS activities(id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, actor_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, kind TEXT NOT NULL, target_id BIGINT, created_at BIGINT NOT NULL);
    CREATE INDEX IF NOT EXISTS messages_pair_idx ON messages(sender_id, receiver_id, created_at);
    CREATE INDEX IF NOT EXISTS profile_views_profile_idx ON profile_views(profile_id, viewed_at DESC);
    CREATE INDEX IF NOT EXISTS posts_created_idx ON posts(created_at DESC);
    CREATE INDEX IF NOT EXISTS stories_expiry_idx ON stories(expires_at);
    '''
    with db() as c: c.executescript(schema)


def init_db():
    if DATABASE_URL:
        init_postgres()
        return
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL, display_name TEXT NOT NULL,
          username TEXT NOT NULL UNIQUE, bio TEXT NOT NULL DEFAULT '', interests TEXT NOT NULL DEFAULT '[]',
          accent TEXT NOT NULL DEFAULT '#ff3038', profile_bg TEXT NOT NULL DEFAULT 'midnight', avatar_path TEXT NOT NULL DEFAULT '', bg_image_path TEXT NOT NULL DEFAULT '',
          created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(
          token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          expires_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS posts(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          media_path TEXT NOT NULL, media_type TEXT NOT NULL, caption TEXT NOT NULL DEFAULT '', sticker_text TEXT NOT NULL DEFAULT '', sticker_date INTEGER NOT NULL DEFAULT 0, audio_name TEXT NOT NULL DEFAULT '', audio_artist TEXT NOT NULL DEFAULT '', audio_source TEXT NOT NULL DEFAULT '', audio_path TEXT NOT NULL DEFAULT '',
          created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS likes(
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
          created_at INTEGER NOT NULL, PRIMARY KEY(user_id, post_id));
        CREATE TABLE IF NOT EXISTS saves(
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
          created_at INTEGER NOT NULL, PRIMARY KEY(user_id, post_id));
        CREATE TABLE IF NOT EXISTS follows(
          follower_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          followed_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          created_at INTEGER NOT NULL, PRIMARY KEY(follower_id, followed_id),
          CHECK(follower_id != followed_id));
        CREATE TABLE IF NOT EXISTS comments(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
          body TEXT NOT NULL, parent_id INTEGER REFERENCES comments(id) ON DELETE CASCADE, created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS stories(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          media_path TEXT NOT NULL, media_type TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS messages(
          id INTEGER PRIMARY KEY AUTOINCREMENT, sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          receiver_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, body TEXT NOT NULL, created_at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS messages_pair_idx ON messages(sender_id, receiver_id, created_at);
        CREATE TABLE IF NOT EXISTS reposts(user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE, created_at INTEGER NOT NULL, PRIMARY KEY(user_id,post_id));
        CREATE TABLE IF NOT EXISTS profile_views(id INTEGER PRIMARY KEY AUTOINCREMENT, profile_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, viewer_id INTEGER REFERENCES users(id) ON DELETE CASCADE, viewed_at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS profile_views_profile_idx ON profile_views(profile_id,viewed_at DESC);
        CREATE TABLE IF NOT EXISTS stickers(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,label TEXT NOT NULL,kind TEXT NOT NULL DEFAULT 'text',created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS activities(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,actor_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,kind TEXT NOT NULL,target_id INTEGER,created_at INTEGER NOT NULL);
        ''')
        # Safe schema migration for databases created by earlier VYRO versions.
        user_columns = {r['name'] for r in c.execute('PRAGMA table_info(users)').fetchall()}
        if 'accent' not in user_columns: c.execute("ALTER TABLE users ADD COLUMN accent TEXT NOT NULL DEFAULT '#ff3038'")
        if 'profile_bg' not in user_columns: c.execute("ALTER TABLE users ADD COLUMN profile_bg TEXT NOT NULL DEFAULT 'midnight'")
        if 'avatar_path' not in user_columns: c.execute("ALTER TABLE users ADD COLUMN avatar_path TEXT NOT NULL DEFAULT ''")
        if 'bg_image_path' not in user_columns: c.execute("ALTER TABLE users ADD COLUMN bg_image_path TEXT NOT NULL DEFAULT ''")
        if 'interests' not in user_columns: c.execute("ALTER TABLE users ADD COLUMN interests TEXT NOT NULL DEFAULT '[]'")
        c.execute("UPDATE users SET accent='#ff3038' WHERE lower(accent)='#a855f7'")
        c.execute("UPDATE users SET profile_bg='midnight' WHERE profile_bg='violet'")
        post_columns = {r['name'] for r in c.execute('PRAGMA table_info(posts)').fetchall()}
        if 'sticker_text' not in post_columns: c.execute("ALTER TABLE posts ADD COLUMN sticker_text TEXT NOT NULL DEFAULT ''")
        if 'sticker_date' not in post_columns: c.execute("ALTER TABLE posts ADD COLUMN sticker_date INTEGER NOT NULL DEFAULT 0")
        for col, decl in [('audio_name', "TEXT NOT NULL DEFAULT ''"), ('audio_artist', "TEXT NOT NULL DEFAULT ''"), ('audio_source', "TEXT NOT NULL DEFAULT ''"), ('audio_path', "TEXT NOT NULL DEFAULT ''")]:
            if col not in post_columns: c.execute(f'ALTER TABLE posts ADD COLUMN {col} {decl}')
        comment_columns = {r['name'] for r in c.execute('PRAGMA table_info(comments)').fetchall()}
        if 'parent_id' not in comment_columns: c.execute('ALTER TABLE comments ADD COLUMN parent_id INTEGER REFERENCES comments(id) ON DELETE CASCADE')
        c.executescript('''
        CREATE INDEX IF NOT EXISTS posts_created_idx ON posts(created_at DESC);
        CREATE INDEX IF NOT EXISTS stories_expiry_idx ON stories(expires_at);
        ''')


def pw_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 310000)
    return 'pbkdf2_sha256$310000$%s$%s' % (salt.hex(), digest.hex())


def pw_verify(password, encoded):
    try:
        scheme, rounds, salt_hex, digest_hex = encoded.split('$')
        if scheme != 'pbkdf2_sha256': return False
        test = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt_hex), int(rounds)).hex()
        return hmac.compare_digest(test, digest_hex)
    except (ValueError, TypeError):
        return False


def public_user(row, con, viewer_id=None):
    uid = row['id']
    followers = con.execute('SELECT COUNT(*) n FROM follows WHERE followed_id=?', (uid,)).fetchone()['n']
    following = con.execute('SELECT COUNT(*) n FROM follows WHERE follower_id=?', (uid,)).fetchone()['n']
    posts = con.execute('SELECT COUNT(*) n FROM posts WHERE user_id=?', (uid,)).fetchone()['n']
    is_following = False
    if viewer_id and viewer_id != uid:
        is_following = con.execute('SELECT 1 FROM follows WHERE follower_id=? AND followed_id=?', (viewer_id, uid)).fetchone() is not None
    return {'id': uid, 'username': row['username'], 'display_name': row['display_name'], 'bio': row['bio'],
            'followers': followers, 'following': following, 'posts': posts, 'is_following': is_following,
            'accent': row['accent'] if 'accent' in row.keys() else '#ff3038',
            'profile_bg': row['profile_bg'] if 'profile_bg' in row.keys() else 'midnight',
            'avatar_url': media_url(row['avatar_path']) if 'avatar_path' in row.keys() and row['avatar_path'] else '',
            'bg_image_url': media_url(row['bg_image_path']) if 'bg_image_path' in row.keys() and row['bg_image_path'] else '',
            'interests': json.loads(row['interests'] or '[]') if 'interests' in row.keys() else [],
            'profile_views': con.execute('SELECT COUNT(*) n FROM profile_views WHERE profile_id=?',(uid,)).fetchone()['n']}


class VYROHandler(BaseHTTPRequestHandler):
    server_version = 'RadeGram/1.0'
    sys_version = ''

    def log_message(self, fmt, *args):
        print('[RadeGram] %s - %s' % (self.address_string(), fmt % args))

    def send_json(self, code, payload, extra_headers=None):
        raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'same-origin')
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' https: blob: data:; media-src 'self' https: blob:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
        if extra_headers:
            for k, v in extra_headers: self.send_header(k, v)
        self.end_headers(); self.wfile.write(raw)

    def error(self, code, message): self.send_json(code, {'error': message})

    def read_body(self, limit=1024*1024):
        try: length = int(self.headers.get('Content-Length', '0'))
        except ValueError: raise ValueError('Geçersiz istek boyutu.')
        if length < 0 or length > limit: raise OverflowError('İstek boyutu sınırı aşıldı.')
        data = self.rfile.read(length)
        if len(data) != length: raise ValueError('İstek tamamlanamadı.')
        return data

    def json_body(self):
        if 'application/json' not in self.headers.get('Content-Type', ''): raise ValueError('JSON istek gövdesi gerekli.')
        data = self.read_body(1024*1024)
        try: result = json.loads(data.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError): raise ValueError('Geçersiz JSON.')
        if not isinstance(result, dict): raise ValueError('Geçersiz istek.')
        return result

    def current_user(self, con):
        cookie = SimpleCookie()
        try: cookie.load(self.headers.get('Cookie', ''))
        except Exception: return None
        morsel = cookie.get(COOKIE_NAME)
        if not morsel: return None
        token_hash = hashlib.sha256(morsel.value.encode()).hexdigest()
        row = con.execute('SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?', (token_hash, int(time.time()))).fetchone()
        return row

    def require_user(self, con):
        user = self.current_user(con)
        if not user: self.error(401, 'Bu işlem için giriş yapmalısın.'); return None
        return user

    def check_origin(self):
        origin = self.headers.get('Origin')
        if not origin: return True
        parsed = urlparse(origin)
        host = self.headers.get('Host', '')
        return parsed.netloc.lower() == host.lower() and parsed.scheme in ('http', 'https')

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        try:
            with db() as con:
                if path == '/api/health': return self.send_json(200, {'ok': True, 'service': 'RadeGram'})
                if path == '/api/me':
                    user = self.current_user(con)
                    return self.send_json(200, {'user': public_user(user, con, user['id']) if user else None})
                if path == '/api/feed':
                    viewer = self.current_user(con); viewer_id = viewer['id'] if viewer else -1
                    rows = con.execute('''SELECT p.*,u.username,u.display_name,u.avatar_path FROM posts p JOIN users u ON u.id=p.user_id ORDER BY p.created_at DESC LIMIT 100''').fetchall()
                    posts=[]
                    for r in rows:
                        posts.append({'id':r['id'],'user_id':r['user_id'],'username':r['username'],'display_name':r['display_name'],
                          'url':media_url(r['media_path']),'media_type':r['media_type'],'caption':r['caption'],'created_at':r['created_at'],'sticker_text':r['sticker_text'] if 'sticker_text' in r.keys() else '', 'sticker_date':bool(r['sticker_date']) if 'sticker_date' in r.keys() else False, 'audio_name':r['audio_name'] if 'audio_name' in r.keys() else '', 'audio_artist':r['audio_artist'] if 'audio_artist' in r.keys() else '', 'audio_source':r['audio_source'] if 'audio_source' in r.keys() else '', 'audio_url':media_url(r['audio_path']) if 'audio_path' in r.keys() and r['audio_path'] else '',
                          'reposts':con.execute('SELECT COUNT(*) n FROM reposts WHERE post_id=?',(r['id'],)).fetchone()['n'], 'reposted':con.execute('SELECT 1 FROM reposts WHERE user_id=? AND post_id=?',(viewer_id,r['id'])).fetchone() is not None,
                          'avatar_url': (media_url(r['avatar_path'])) if 'avatar_path' in r.keys() and r['avatar_path'] else '',
                          'likes':con.execute('SELECT COUNT(*) n FROM likes WHERE post_id=?',(r['id'],)).fetchone()['n'],
                          'comments':con.execute('SELECT COUNT(*) n FROM comments WHERE post_id=?',(r['id'],)).fetchone()['n'],
                          'liked':con.execute('SELECT 1 FROM likes WHERE user_id=? AND post_id=?',(viewer_id,r['id'])).fetchone() is not None,
                          'saved':con.execute('SELECT 1 FROM saves WHERE user_id=? AND post_id=?',(viewer_id,r['id'])).fetchone() is not None,
                          'is_owner':viewer_id==r['user_id']})
                    return self.send_json(200, {'posts':posts})
                if path == '/api/saved' or path == '/api/liked':
                    user=self.require_user(con)
                    if not user:return
                    table='saves' if path.endswith('saved') else 'likes'
                    rows=con.execute(f'''SELECT p.*,u.username,u.display_name FROM {table} x JOIN posts p ON p.id=x.post_id JOIN users u ON u.id=p.user_id WHERE x.user_id=? ORDER BY x.created_at DESC''',(user['id'],)).fetchall()
                    return self.send_json(200, {'posts':[self.post_dict(con,r,user['id']) for r in rows]})
                if path == '/api/memories':
                    user=self.current_user(con)
                    target_q=parse_qs(urlparse(self.path).query).get('user_id',[''])[0]
                    try: target=int(target_q) if target_q else (user['id'] if user else 0)
                    except ValueError: target=0
                    if not target:return self.error(400,'Profil seçilmedi.')
                    rows=con.execute('SELECT s.*,u.username,u.display_name FROM stories s JOIN users u ON u.id=s.user_id WHERE s.user_id=? ORDER BY s.created_at DESC LIMIT 200',(target,)).fetchall()
                    return self.send_json(200,{'memories':[{'id':r['id'],'user_id':r['user_id'],'username':r['username'],'display_name':r['display_name'],'url':media_url(r['media_path']),'media_type':r['media_type'],'created_at':r['created_at'],'expired':r['expires_at']<=int(time.time())} for r in rows]})
                if path == '/api/stories':
                    now=int(time.time())
                    rows=con.execute('SELECT s.*,u.username,u.display_name FROM stories s JOIN users u ON u.id=s.user_id WHERE s.expires_at>? ORDER BY s.created_at DESC LIMIT 100',(now,)).fetchall()
                    return self.send_json(200, {'stories':[{'id':r['id'],'username':r['username'],'display_name':r['display_name'],'url':media_url(r['media_path']),'media_type':r['media_type'],'created_at':r['created_at']} for r in rows]})
                if path == '/api/search':
                    q = parse_qs(urlparse(self.path).query).get('q', [''])[0].strip().lower()[:80]
                    viewer = self.current_user(con); viewer_id = viewer['id'] if viewer else None
                    if not q:
                        return self.send_json(200, {'users': [], 'posts': []})
                    pattern = '%' + q.replace('%', r'\%').replace('_', r'\_') + '%'
                    urows = con.execute("SELECT * FROM users WHERE lower(username) LIKE ? ESCAPE '\\' OR lower(display_name) LIKE ? ESCAPE '\\' ORDER BY username LIMIT 30", (pattern, pattern)).fetchall()
                    prows = con.execute("SELECT p.*,u.username,u.display_name,u.avatar_path FROM posts p JOIN users u ON u.id=p.user_id WHERE lower(p.caption) LIKE ? ESCAPE '\\' OR lower(u.username) LIKE ? ESCAPE '\\' OR lower(u.display_name) LIKE ? ESCAPE '\\' ORDER BY p.created_at DESC LIMIT 50", (pattern, pattern, pattern)).fetchall()
                    return self.send_json(200, {'users': [public_user(r, con, viewer_id) for r in urows], 'posts': [self.post_dict(con, r, viewer_id if viewer_id is not None else -1) for r in prows]})
                if path == '/api/reposts':
                    user=self.require_user(con)
                    if not user:return
                    target_q=parse_qs(urlparse(self.path).query).get('user_id',[''])[0]
                    try:target=int(target_q) if target_q else user['id']
                    except ValueError:target=user['id']
                    rows=con.execute('SELECT p.*,u.username,u.display_name,u.avatar_path FROM reposts x JOIN posts p ON p.id=x.post_id JOIN users u ON u.id=p.user_id WHERE x.user_id=? ORDER BY x.created_at DESC',(target,)).fetchall()
                    return self.send_json(200,{'posts':[self.post_dict(con,r,user['id']) for r in rows]})
                if path == '/api/stickers':
                    user=self.require_user(con)
                    if not user:return
                    rows=con.execute('SELECT id,label,kind,created_at FROM stickers WHERE user_id=? ORDER BY created_at DESC LIMIT 100',(user['id'],)).fetchall()
                    return self.send_json(200,{'stickers':[dict(r) for r in rows]})
                if path == '/api/sounds':
                    q=parse_qs(urlparse(self.path).query).get('q',[''])[0].strip().lower()[:80]
                    rows=con.execute("SELECT audio_name,audio_artist,audio_source,COUNT(*) uses,MAX(id) latest_post,(SELECT audio_path FROM posts p2 WHERE p2.audio_name=p.audio_name AND p2.audio_artist=p.audio_artist AND p2.audio_source=p.audio_source AND p2.audio_path<>'' ORDER BY p2.id DESC LIMIT 1) audio_path FROM posts p WHERE audio_name<>'' AND (lower(audio_name) LIKE ? OR lower(audio_artist) LIKE ?) GROUP BY audio_name,audio_artist,audio_source ORDER BY uses DESC LIMIT 50",('%'+q+'%','%'+q+'%')).fetchall()
                    return self.send_json(200,{'sounds':[{'audio_name':r['audio_name'],'audio_artist':r['audio_artist'],'audio_source':r['audio_source'],'uses':r['uses'],'latest_post':r['latest_post'],'audio_url':media_url(r['audio_path']) if r['audio_path'] else ''} for r in rows]})
                if path == '/api/tags':
                    q=parse_qs(urlparse(self.path).query).get('q',[''])[0].strip().lstrip('#').lower()[:50]
                    if not q:return self.send_json(200,{'tag':q,'count':0,'posts':[]})
                    pattern='%#'+q+'%'
                    total=con.execute('SELECT COUNT(*) n FROM posts WHERE lower(caption) LIKE ?',(pattern,)).fetchone()['n']
                    rows=con.execute('SELECT p.*,u.username,u.display_name,u.avatar_path FROM posts p JOIN users u ON u.id=p.user_id WHERE lower(p.caption) LIKE ? ORDER BY p.created_at DESC LIMIT 50',(pattern,)).fetchall()
                    return self.send_json(200,{'tag':q,'count':total,'posts':[self.post_dict(con,r,-1) for r in rows]})
                match=re.fullmatch(r'/api/users/([0-9]+)/views',path)
                if match:
                    user=self.require_user(con)
                    if not user:return
                    target=int(match.group(1))
                    if target!=user['id']:return self.error(403,'Görüntüleyen listesi yalnızca profil sahibine açıktır.')
                    rows=con.execute('SELECT v.viewed_at,u.id,u.username,u.display_name,u.avatar_path FROM profile_views v LEFT JOIN users u ON u.id=v.viewer_id WHERE v.profile_id=? ORDER BY v.viewed_at DESC LIMIT 100',(target,)).fetchall()
                    return self.send_json(200,{'views':[{'viewed_at':r['viewed_at'],'user_id':r['id'],'username':r['username'] or 'Misafir','display_name':r['display_name'] or 'Misafir','avatar_url':media_url(r['avatar_path']) if r['avatar_path'] else ''} for r in rows]})
                match=re.fullmatch(r'/api/users/([0-9]+)/?',path)
                if match:
                    row=con.execute('SELECT * FROM users WHERE id=?',(int(match.group(1)),)).fetchone()
                    if not row:return self.error(404,'Kullanıcı bulunamadı.')
                    viewer=self.current_user(con)
                    return self.send_json(200,{'user':public_user(row,con,viewer['id'] if viewer else None)})
                if path == '/api/activities':
                    user=self.require_user(con)
                    if not user:return
                    rows=con.execute('SELECT a.kind,a.target_id,a.created_at,u.username,u.display_name FROM activities a JOIN users u ON u.id=a.actor_id WHERE a.user_id=? ORDER BY a.created_at DESC LIMIT 100',(user['id'],)).fetchall()
                    return self.send_json(200,{'activities':[dict(r) for r in rows]})
                if path == '/api/conversations':
                    user=self.require_user(con)
                    if not user:return
                    rows=con.execute('''SELECT u.id,u.username,u.display_name,u.avatar_path,m.body,m.created_at,
                      m.sender_id FROM messages m JOIN users u ON u.id=CASE WHEN m.sender_id=? THEN m.receiver_id ELSE m.sender_id END
                      WHERE m.id IN (SELECT MAX(id) FROM messages WHERE sender_id=? OR receiver_id=? GROUP BY CASE WHEN sender_id=? THEN receiver_id ELSE sender_id END)
                      ORDER BY m.created_at DESC LIMIT 100''',(user['id'],user['id'],user['id'],user['id'])).fetchall()
                    return self.send_json(200,{'conversations':[{'user_id':r['id'],'username':r['username'],'display_name':r['display_name'],'avatar_url':media_url(r['avatar_path']) if r['avatar_path'] else '', 'last_message':r['body'],'created_at':r['created_at'],'mine':r['sender_id']==user['id']} for r in rows]})
                if path == '/api/messages':
                    user=self.require_user(con)
                    if not user:return
                    target=parse_qs(urlparse(self.path).query).get('with',[''])[0]
                    try: target_id=int(target)
                    except ValueError:return self.error(400,'Sohbet kullanıcı numarası geçersiz.')
                    if not con.execute('SELECT 1 FROM users WHERE id=?',(target_id,)).fetchone():return self.error(404,'Kullanıcı bulunamadı.')
                    rows=con.execute('''SELECT m.*,u.username,u.display_name FROM messages m JOIN users u ON u.id=m.sender_id
                      WHERE (m.sender_id=? AND m.receiver_id=?) OR (m.sender_id=? AND m.receiver_id=?) ORDER BY m.created_at ASC LIMIT 500''',(user['id'],target_id,target_id,user['id'])).fetchall()
                    return self.send_json(200,{'messages':[{'id':r['id'],'sender_id':r['sender_id'],'body':r['body'],'created_at':r['created_at'],'username':r['username'],'display_name':r['display_name'],'mine':r['sender_id']==user['id']} for r in rows]})
                match=re.fullmatch(r'/api/posts/([0-9]+)/comments',path)
                if match:
                    rows=con.execute('SELECT c.id,c.user_id,c.body,c.parent_id,c.created_at,u.username,u.display_name,pu.username parent_username,pc.body parent_body FROM comments c JOIN users u ON u.id=c.user_id LEFT JOIN comments pc ON pc.id=c.parent_id LEFT JOIN users pu ON pu.id=pc.user_id WHERE c.post_id=? ORDER BY c.created_at ASC LIMIT 200',(int(match.group(1)),)).fetchall()
                    return self.send_json(200,{'comments':[dict(r) for r in rows]})
            if path.startswith('/uploads/'):
                return self.serve_upload(path)
            if path == '/' or path == '/index.html': return self.serve_file(RESOURCE_DIR/'index.html')
            return self.error(404,'Sayfa bulunamadı.')
        except Exception as e:
            print('[RadeGram] GET error:',repr(e)); return self.error(500,'Sunucu hatası.')

    def post_dict(self, con, r, viewer_id):
        return {'id':r['id'],'user_id':r['user_id'],'username':r['username'],'display_name':r['display_name'],'url':media_url(r['media_path']),
          'media_type':r['media_type'],'caption':r['caption'],'created_at':r['created_at'],'sticker_text':r['sticker_text'] if 'sticker_text' in r.keys() else '', 'sticker_date':bool(r['sticker_date']) if 'sticker_date' in r.keys() else False, 'audio_name':r['audio_name'] if 'audio_name' in r.keys() else '', 'audio_artist':r['audio_artist'] if 'audio_artist' in r.keys() else '', 'audio_source':r['audio_source'] if 'audio_source' in r.keys() else '', 'audio_url':media_url(r['audio_path']) if 'audio_path' in r.keys() and r['audio_path'] else '',
          'reposts':con.execute('SELECT COUNT(*) n FROM reposts WHERE post_id=?',(r['id'],)).fetchone()['n'], 'reposted':con.execute('SELECT 1 FROM reposts WHERE user_id=? AND post_id=?',(viewer_id,r['id'])).fetchone() is not None,
          'avatar_url': (media_url(r['avatar_path'])) if 'avatar_path' in r.keys() and r['avatar_path'] else '',
          'likes':con.execute('SELECT COUNT(*) n FROM likes WHERE post_id=?',(r['id'],)).fetchone()['n'],
          'comments':con.execute('SELECT COUNT(*) n FROM comments WHERE post_id=?',(r['id'],)).fetchone()['n'],
          'liked':con.execute('SELECT 1 FROM likes WHERE user_id=? AND post_id=?',(viewer_id,r['id'])).fetchone() is not None,
          'saved':con.execute('SELECT 1 FROM saves WHERE user_id=? AND post_id=?',(viewer_id,r['id'])).fetchone() is not None,'is_owner':viewer_id==r['user_id']}

    def serve_file(self, path):
        if not path.is_file(): return self.error(404,'index.html bulunamadı.')
        raw=path.read_bytes();self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(raw)));self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','same-origin');self.send_header('Cache-Control','no-cache');self.end_headers();self.wfile.write(raw)

    def serve_upload(self, path):
        rel=path.removeprefix('/uploads/')
        if not re.fullmatch(r'[a-f0-9]{32}\.(mp4|webm|mov|m4v|png|jpg|jpeg|webp|gif|mp3|m4a|wav|ogg)',rel):return self.error(404,'Dosya bulunamadı.')
        f=(UPLOAD_DIR/rel).resolve()
        if f.parent != UPLOAD_DIR.resolve() or not f.is_file():return self.error(404,'Dosya bulunamadı.')
        ctype=mimetypes.guess_type(f.name)[0] or 'application/octet-stream';size=f.stat().st_size
        self.send_response(200);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(size));self.send_header('X-Content-Type-Options','nosniff');self.send_header('Cache-Control','public, max-age=3600');self.send_header('Accept-Ranges','bytes');self.end_headers()
        with f.open('rb') as fp:
            while True:
                chunk=fp.read(1024*1024)
                if not chunk:break
                self.wfile.write(chunk)

    def save_session(self, user_id):
        token=secrets.token_urlsafe(32); token_hash=hashlib.sha256(token.encode()).hexdigest(); expires=int(time.time())+SESSION_DAYS*86400
        with db() as con: con.execute('INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)',(token_hash,user_id,expires))
        cookie=f'{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_DAYS*86400}' + ('; Secure' if SECURE_COOKIE else '')
        return [('Set-Cookie',cookie)]

    def multipart(self):
        ctype=self.headers.get('Content-Type','')
        if 'multipart/form-data' not in ctype: raise ValueError('Dosya yüklemek için multipart/form-data gerekli.')
        length=int(self.headers.get('Content-Length','0'))
        if length<=0 or length>MAX_UPLOAD+1024*1024: raise OverflowError(f'Dosya en fazla {MAX_UPLOAD//(1024*1024)} MB olabilir.')
        body=self.rfile.read(length)
        if len(body)!=length: raise ValueError('Dosya yüklemesi tamamlanamadı.')
        msg=BytesParser(policy=default).parsebytes(('Content-Type: '+ctype+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+body)
        fields={}
        for part in msg.iter_parts():
            name=part.get_param('name',header='content-disposition')
            if not name:continue
            filename=part.get_filename()
            payload=part.get_payload(decode=True) or b''
            if filename:fields[name]={'filename':filename,'type':part.get_content_type(),'data':payload}
            else:fields[name]=payload.decode('utf-8','replace')
        return fields

    def do_POST(self):
        if not self.check_origin():return self.error(403,'İstek kaynağı doğrulanamadı.')
        path=urlparse(self.path).path
        try:
            if path in ('/api/register','/api/login','/api/logout'):
                data=self.json_body()
                with db() as con:
                    if path=='/api/register':
                        email=str(data.get('email','')).strip().lower();password=str(data.get('password',''));name=str(data.get('display_name','')).strip();username=str(data.get('username','')).strip().lower()
                        if len(email)>254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email):return self.error(400,'Geçerli bir e-posta gir.')
                        if len(password)<10 or len(password)>128:return self.error(400,'Şifre en az 10, en fazla 128 karakter olmalı.')
                        if not re.fullmatch(r'[a-z0-9_]{3,24}',username):return self.error(400,'Kullanıcı adı 3-24 karakter; küçük harf, rakam ve _ kullanabilir.')
                        if not name or len(name)>50:return self.error(400,'Görünen ad 1-50 karakter olmalı.')
                        try:
                            cur=con.execute('INSERT INTO users(email,password_hash,display_name,username,created_at) VALUES(?,?,?,?,?)',(email,pw_hash(password),name,username,int(time.time())))
                            uid=cur.lastrowid
                        except INTEGRITY_ERRORS:return self.error(409,'Bu e-posta veya kullanıcı adı zaten kullanılıyor.')
                    elif path=='/api/login':
                        email=str(data.get('email','')).strip().lower();password=str(data.get('password',''))
                        row=con.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
                        if not row or not pw_verify(password,row['password_hash']):return self.error(401,'E-posta veya şifre hatalı.')
                        uid=row['id']
                    else:
                        cookie=SimpleCookie();cookie.load(self.headers.get('Cookie',''));m=cookie.get(COOKIE_NAME)
                        if m:con.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(m.value.encode()).hexdigest(),))
                        expired=f'{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0'+('; Secure' if SECURE_COOKIE else '')
                        return self.send_json(200,{'ok':True},[('Set-Cookie',expired)])
                headers=self.save_session(uid)
                with db() as con: user=con.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); payload={'ok':True,'user':public_user(user,con,uid)}
                return self.send_json(200,payload,headers)

            if path in ('/api/upload','/api/story'):
                fields=self.multipart()
                with db() as con:
                    user=self.require_user(con)
                    if not user:return
                    media=fields.get('file')
                    if not isinstance(media,dict) or not media['data']:return self.error(400,'Bir dosya seçmelisin.')
                    typ=media['type'].lower(); filename=media['filename'].lower()
                    if path=='/api/upload':
                        allowed={'video/mp4':'.mp4','video/webm':'.webm','video/quicktime':'.mov','video/x-m4v':'.m4v','image/jpeg':'.jpg','image/png':'.png','image/webp':'.webp'}
                        if typ not in allowed:return self.error(415,'Gönderi için MP4, WebM, MOV, JPG, PNG veya WEBP kullan.')
                        ext=allowed[typ];media_type='video' if typ.startswith('video/') else 'image'
                    else:
                        allowed={'video/mp4':'.mp4','video/webm':'.webm','video/quicktime':'.mov','image/jpeg':'.jpg','image/png':'.png','image/webp':'.webp','image/gif':'.gif'}
                        if typ not in allowed:return self.error(415,'Hikâye için desteklenen görsel/video formatını kullan.')
                        ext=allowed[typ];media_type='video' if typ.startswith('video/') else 'image'
                    if len(media['data'])>MAX_UPLOAD:return self.error(413,'Dosya boyutu sınırı aşıldı.')
                    file_id=secrets.token_hex(16);safe_name=file_id+ext;rel=save_upload(media['data'], safe_name, typ);now=int(time.time())
                    if path=='/api/upload':
                        caption=str(fields.get('caption','')).strip()[:500]
                        sticker_text=str(fields.get('sticker_text','')).strip()[:80]
                        sticker_date=1 if str(fields.get('sticker_date','')).lower() in ('1','true','on','yes') else 0
                        audio_name=str(fields.get('audio_name','')).strip()[:100];audio_artist=str(fields.get('audio_artist','')).strip()[:100];audio_source=str(fields.get('audio_source','')).strip()[:200];audio_path=''
                        existing_audio=str(fields.get('audio_existing_path','')).strip()
                        if existing_audio.startswith(('https://','http://')):
                            audio_path=existing_audio
                        elif re.fullmatch(r'/?uploads/[a-f0-9]{32}\.(mp3|m4a|wav|ogg|webm)',existing_audio):
                            candidate=(UPLOAD_DIR/Path(existing_audio).name).resolve()
                            if candidate.parent==UPLOAD_DIR.resolve() and candidate.is_file():audio_path='uploads/'+Path(existing_audio).name
                        audio=fields.get('audio_file')
                        if isinstance(audio,dict) and audio.get('data'):
                            at=audio['type'].lower();aext={'audio/mpeg':'.mp3','audio/mp3':'.mp3','audio/mp4':'.m4a','audio/x-m4a':'.m4a','audio/wav':'.wav','audio/x-wav':'.wav','audio/ogg':'.ogg','audio/webm':'.webm'}.get(at)
                            if not aext:return self.error(415,'Ses dosyası MP3, M4A, WAV, OGG veya WebM olmalı.')
                            if len(audio['data'])>50*1024*1024:return self.error(413,'Ses dosyası en fazla 50 MB olabilir.')
                            aname=secrets.token_hex(16)+aext
                            audio_path=save_upload(audio['data'], aname, at)
                        cur=con.execute('INSERT INTO posts(user_id,media_path,media_type,caption,sticker_text,sticker_date,audio_name,audio_artist,audio_source,audio_path,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(user['id'],rel,media_type,caption,sticker_text,sticker_date,audio_name,audio_artist,audio_source,audio_path,now));item_id=cur.lastrowid
                        return self.send_json(201,{'ok':True,'id':item_id,'url':media_url(rel)})
                    cur=con.execute('INSERT INTO stories(user_id,media_path,media_type,created_at,expires_at) VALUES(?,?,?,?,?)',(user['id'],rel,media_type,now,now+86400));return self.send_json(201,{'ok':True,'id':cur.lastrowid,'url':media_url(rel),'expires_at':now+86400})

            match=re.fullmatch(r'/api/posts/(\d+)/(like|save|comments)',path)
            if match:
                post_id=int(match.group(1));action=match.group(2);data=self.json_body()
                with db() as con:
                    user=self.require_user(con)
                    if not user:return
                    if not con.execute('SELECT 1 FROM posts WHERE id=?',(post_id,)).fetchone():return self.error(404,'Gönderi bulunamadı.')
                    if action in ('like','save'):
                        table='likes' if action=='like' else 'saves'
                        existing=con.execute(f'SELECT 1 FROM {table} WHERE user_id=? AND post_id=?',(user['id'],post_id)).fetchone()
                        if existing:con.execute(f'DELETE FROM {table} WHERE user_id=? AND post_id=?',(user['id'],post_id));active=False
                        else:con.execute(f'INSERT INTO {table}(user_id,post_id,created_at) VALUES(?,?,?)',(user['id'],post_id,int(time.time())));active=True
                        return self.send_json(200,{'ok':True,'active':active})
                    body=str(data.get('body','')).strip();parent_id=data.get('parent_id')
                    if not body or len(body)>1000:return self.error(400,'Yorum 1-1000 karakter olmalı.')
                    if parent_id is not None:
                        try:parent_id=int(parent_id)
                        except (ValueError,TypeError):return self.error(400,'Yanıtlanan yorum geçersiz.')
                        if not con.execute('SELECT 1 FROM comments WHERE id=? AND post_id=?',(parent_id,post_id)).fetchone():return self.error(400,'Yanıtlanan yorum bulunamadı.')
                    cur=con.execute('INSERT INTO comments(user_id,post_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(user['id'],post_id,body,parent_id,int(time.time())))
                    return self.send_json(201,{'ok':True,'id':cur.lastrowid,'body':body,'username':user['username'],'parent_id':parent_id})
            match=re.fullmatch(r'/api/posts/(\d+)/repost',path)
            if match:
                post_id=int(match.group(1))
                with db() as con:
                    user=self.require_user(con)
                    if not user:return
                    if not con.execute('SELECT 1 FROM posts WHERE id=?',(post_id,)).fetchone():return self.error(404,'Gönderi bulunamadı.')
                    old=con.execute('SELECT 1 FROM reposts WHERE user_id=? AND post_id=?',(user['id'],post_id)).fetchone()
                    if old:con.execute('DELETE FROM reposts WHERE user_id=? AND post_id=?',(user['id'],post_id));active=False
                    else:
                        now=int(time.time());con.execute('INSERT INTO reposts(user_id,post_id,created_at) VALUES(?,?,?)',(user['id'],post_id,now));owner=con.execute('SELECT user_id FROM posts WHERE id=?',(post_id,)).fetchone()['user_id'];con.execute('INSERT INTO activities(user_id,actor_id,kind,target_id,created_at) VALUES(?,?,?,?,?)',(owner,user['id'],'repost',post_id,now));active=True
                    return self.send_json(200,{'ok':True,'active':active})
            match=re.fullmatch(r'/api/users/(\d+)/view',path)
            if match:
                target=int(match.group(1))
                with db() as con:
                    viewer=self.current_user(con)
                    if not con.execute('SELECT 1 FROM users WHERE id=?',(target,)).fetchone():return self.error(404,'Profil bulunamadı.')
                    if viewer and viewer['id']!=target:
                        now=int(time.time());last=con.execute('SELECT viewed_at FROM profile_views WHERE profile_id=? AND viewer_id=? ORDER BY viewed_at DESC LIMIT 1',(target,viewer['id'])).fetchone()
                        if not last or now-last['viewed_at']>3600:
                            con.execute('INSERT INTO profile_views(profile_id,viewer_id,viewed_at) VALUES(?,?,?)',(target,viewer['id'],now));con.execute('INSERT INTO activities(user_id,actor_id,kind,target_id,created_at) VALUES(?,?,?,?,?)',(target,viewer['id'],'view',target,now))
                    return self.send_json(200,{'ok':True})
            if path == '/api/stickers':
                data=self.json_body()
                with db() as con:
                    user=self.require_user(con)
                    if not user:return
                    label=str(data.get('label','')).strip()[:80];kind=str(data.get('kind','text'))
                    if not label:return self.error(400,'Sticker yazısı boş olamaz.')
                    if kind not in ('text','emoji','date'):return self.error(400,'Sticker türü geçersiz.')
                    cur=con.execute('INSERT INTO stickers(user_id,label,kind,created_at) VALUES(?,?,?,?)',(user['id'],label,kind,int(time.time())))
                    return self.send_json(201,{'ok':True,'id':cur.lastrowid,'label':label,'kind':kind})
            match=re.fullmatch(r'/api/users/(\d+)/follow',path)
            if match:
                target=int(match.group(1))
                with db() as con:
                    user=self.require_user(con)
                    if not user:return
                    if target==user['id']:return self.error(400,'Kendini takip edemezsin.')
                    if not con.execute('SELECT 1 FROM users WHERE id=?',(target,)).fetchone():return self.error(404,'Kullanıcı bulunamadı.')
                    exists=con.execute('SELECT 1 FROM follows WHERE follower_id=? AND followed_id=?',(user['id'],target)).fetchone()
                    if exists:con.execute('DELETE FROM follows WHERE follower_id=? AND followed_id=?',(user['id'],target));active=False
                    else:
                        now=int(time.time());con.execute('INSERT INTO follows(follower_id,followed_id,created_at) VALUES(?,?,?)',(user['id'],target,now));con.execute('INSERT INTO activities(user_id,actor_id,kind,target_id,created_at) VALUES(?,?,?,?,?)',(target,user['id'],'follow',target,now));active=True
                    return self.send_json(200,{'ok':True,'active':active})
            if path == '/api/interests':
                data=self.json_body()
                with db() as con:
                    user=self.require_user(con)
                    if not user:return
                    interests=data.get('interests',[])
                    allowed={'Oyun','Müzik','Komedi','Dans','Spor','Teknoloji','Yemek','Sanat','Moda','Doğa','Eğitim','Vlog','Edit','Minecraft'}
                    if not isinstance(interests,list):return self.error(400,'İlgi alanları geçersiz.')
                    interests=list(dict.fromkeys(str(x) for x in interests if str(x) in allowed))[:10]
                    con.execute('UPDATE users SET interests=? WHERE id=?',(json.dumps(interests,ensure_ascii=False),user['id']))
                    return self.send_json(200,{'ok':True,'interests':interests})
            if path == '/api/messages':
                data=self.json_body()
                with db() as con:
                    user=self.require_user(con)
                    if not user:return
                    try: target=int(data.get('to_user_id'))
                    except (ValueError,TypeError):return self.error(400,'Alıcı seçilmedi.')
                    body=str(data.get('body','')).strip()
                    if target==user['id']:return self.error(400,'Kendine mesaj gönderemezsin.')
                    if not con.execute('SELECT 1 FROM users WHERE id=?',(target,)).fetchone():return self.error(404,'Kullanıcı bulunamadı.')
                    if not body or len(body)>2000:return self.error(400,'Mesaj 1-2000 karakter olmalı.')
                    cur=con.execute('INSERT INTO messages(sender_id,receiver_id,body,created_at) VALUES(?,?,?,?)',(user['id'],target,body,int(time.time())))
                    return self.send_json(201,{'ok':True,'id':cur.lastrowid,'created_at':int(time.time())})
            if path == '/api/profile-media':
                fields = self.multipart()
                with db() as con:
                    user = self.require_user(con)
                    if not user: return
                    kind = str(fields.get('kind', '')).strip()
                    media = fields.get('file')
                    if kind not in ('avatar', 'background'): return self.error(400, 'Profil görseli türü geçersiz.')
                    if not isinstance(media, dict) or not media.get('data'): return self.error(400, 'Galeriden bir görsel seç.')
                    typ = media['type'].lower()
                    allowed = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp', 'image/gif': '.gif'}
                    if typ not in allowed: return self.error(415, 'JPG, PNG, WEBP veya GIF görseli seç.')
                    max_image = 12 * 1024 * 1024
                    if len(media['data']) > max_image: return self.error(413, 'Profil görseli en fazla 12 MB olabilir.')
                    file_id = secrets.token_hex(16); rel = save_upload(media['data'], file_id + allowed[typ], typ)
                    col = 'avatar_path' if kind == 'avatar' else 'bg_image_path'
                    con.execute(f'UPDATE users SET {col}=? WHERE id=?', (rel, user['id']))
                    return self.send_json(200, {'ok': True, 'url': media_url(rel)})

            if path=='/api/profile':
                data=self.json_body()
                with db() as con:
                    user=self.require_user(con)
                    if not user:return
                    name=str(data.get('display_name','')).strip();username=str(data.get('username','')).strip().lower();bio=str(data.get('bio','')).strip()
                    accent=str(data.get('accent','#ff3038')); profile_bg=str(data.get('profile_bg','midnight'))
                    if not re.fullmatch(r'#[0-9a-fA-F]{6}',accent): return self.error(400,'Geçersiz vurgu rengi.')
                    if profile_bg not in ('midnight','aurora','violet','plain'): return self.error(400,'Geçersiz profil arka planı.')
                    if not name or len(name)>50:return self.error(400,'Görünen ad 1-50 karakter olmalı.')
                    if not re.fullmatch(r'[a-z0-9_]{3,24}',username):return self.error(400,'Kullanıcı adı 3-24 karakter olmalı.')
                    if len(bio)>160:return self.error(400,'Biyografi en fazla 160 karakter olabilir.')
                    try:con.execute('UPDATE users SET display_name=?,username=?,bio=?,accent=?,profile_bg=? WHERE id=?',(name,username,bio,accent,profile_bg,user['id']))
                    except INTEGRITY_ERRORS:return self.error(409,'Bu kullanıcı adı kullanılıyor.')
                    updated=con.execute('SELECT * FROM users WHERE id=?',(user['id'],)).fetchone()
                    return self.send_json(200,{'ok':True,'user':public_user(updated,con,user['id'])})
            return self.error(404,'API adresi bulunamadı.')
        except OverflowError as e:return self.error(413,str(e))
        except ValueError as e:return self.error(400,str(e))
        except Exception as e:
            print('[RadeGram] POST error:',repr(e));return self.error(500,'Sunucu hatası.')

    def do_OPTIONS(self):
        self.send_response(204);self.send_header('Allow','GET, POST, OPTIONS');self.send_header('Content-Length','0');self.end_headers()


def main():
    init_db()
    print('RadeGram API ve arayüz başlatılıyor...')
    print(f'Yerel adres: http://127.0.0.1:{PORT}')
    print(f'Database: {DB_PATH}')
    print(f'Video sınırı: {MAX_UPLOAD//(1024*1024)} MB')
    if HOST == '0.0.0.0': print('UYARI: Bu sunucuyu internete doğrudan açmayın; HTTPS reverse proxy kullanın.')
    server=ThreadingHTTPServer((HOST,PORT),VYROHandler)
    open_browser = os.environ.get('RADEGRAM_OPEN_BROWSER', '1' if getattr(sys, 'frozen', False) else '0') == '1'
    if open_browser and HOST in ('127.0.0.1', 'localhost'):
        threading.Timer(1.0, lambda: webbrowser.open(f'http://127.0.0.1:{PORT}')).start()
    try:server.serve_forever()
    except KeyboardInterrupt:print('\nRadeGram kapatılıyor...')
    finally:server.server_close()

if __name__=='__main__':main()
