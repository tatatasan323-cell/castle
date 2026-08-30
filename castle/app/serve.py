"""入口②：これから発生するもの。薄いローカルサーバ1本で、フォームと画面を出す。

  python castle/app/serve.py            → http://127.0.0.1:8765

  /       ダッシュボード（毎回その場で作り直すので、常に今のDBを映す）
  /note   申し送りフォーム ── ダッシュボードが答えない「なぜ」を、現場が書く口
  /knowledge  知識の泉 ── この会社で繰り返し効くことを、引ける形で貯める
  /login  アクセスキーを入れる ／ /logout  閉じる

認証は users.json の有無で入り切りする（[[users.py]]）。
無ければ認証なしで、入力者名は自己申告。127.0.0.1 に閉じていることだけが防御。
有れば全員に鍵が要り、記録される名前は名乗りではなく鍵の持ち主になる。
**--host を 127.0.0.1 の外にするなら、利用者の登録が必須**（guard_exposure で止める）。

HTTPSではない。社内網に出すならプロキシでHTTPSを終端すること。

申し送りは追記のみ。書き換えも削除もこの画面からはできない ──
取り消せない操作を自動の側に置かない、の適用。訂正は新しく書き足す。
"""

import argparse
import datetime
import html
import http.cookies
import http.server
import json
import pathlib
import string
import sys
import time
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import build_dashboard
import config as config_mod
import db
import knowledge
import users
from normalize import parse_date

NOTE_TEMPLATE = db.ROOT / "castle" / "templates" / "note.html"
LOGIN_TEMPLATE = db.ROOT / "castle" / "templates" / "login.html"
GUIDE_TEMPLATE = db.ROOT / "castle" / "templates" / "guide.html"
KNOWLEDGE_TEMPLATE = db.ROOT / "castle" / "templates" / "knowledge.html"
THEME = db.ROOT / "castle" / "templates" / "theme.css"


def theme():
    """見た目は1枚。ビルド時に差し込むので、実行時の外部読み込みはゼロのまま。"""
    return THEME.read_text(encoding="utf-8")
KIND = "申し送り"
MAX_TEXT = 400
MAX_AUTHOR = 30
COOKIE = "castle_key"
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def guard_exposure(instance, host, acknowledged=False):
    """127.0.0.1 の外に開くときの門。戻り値は止める理由（Noneなら開いてよい）。

    条件は二つ。**認証が入っていること**と、**開く意思が明示されていること**。
    鍵が1本あるだけで通すと、打ち間違いやコピペで機械が社内網に出てしまう。
    ネットワークに出すのは取り消しの効かない側の操作なので、必ず人が明示する。
    """
    if host in LOCAL_HOSTS:
        return None
    if not users.enabled(instance):
        return ("%s に開こうとしています。利用者が登録されていないので開けません。\n"
                "  先に  python castle/app/users.py --add \"名前\"  で鍵を発行してください。" % host)
    if not acknowledged:
        return ("%s に開こうとしています。HTTPSではないので、社内網でも中身は平文で流れます。\n"
                "  承知のうえなら --expose を付けてください。" % host)
    return None


# ---------------------------------------------------------------- 記録

def add_note(conn, cfg, payload):
    """検証して1件足す。戻り値は (通ったか, 人に見せる文)。

    境界（人の入力）でだけ検証する。ここを抜けたものは信頼して扱う。
    """
    dept = cfg.resolve(name=(payload.get("subject") or "").strip())
    if dept is None:
        return False, "部門「%s」は対応表にありません。" % (payload.get("subject") or "（未選択）")

    occurred_at = parse_date(payload.get("occurred_at", ""))
    if occurred_at is None:
        return False, "対象日が読めません。"
    if occurred_at > datetime.date.today().isoformat():
        return False, "対象日が未来になっています。"

    category = (payload.get("category") or "").strip()
    if category not in cfg.note_categories:
        return False, "区分「%s」は一覧にありません。" % (category or "（未選択）")

    author = (payload.get("author") or "").strip()
    if not 1 <= len(author) <= MAX_AUTHOR:
        return False, "入力者名は1〜%d文字で入れてください。" % MAX_AUTHOR

    text = (payload.get("text") or "").strip()
    if not text:
        return False, "内容が空です。"
    if len(text) > MAX_TEXT:
        return False, "内容は%d文字までです（いまは%d文字）。" % (MAX_TEXT, len(text))

    now = datetime.datetime.now().isoformat(timespec="seconds")
    # _key を付けない ＝ 同じ部門・同じ日に何件でも書ける（上書きしない）
    body = {"text": text, "category": category, "source": "form"}
    conn.execute(
        """INSERT INTO records(kind, occurred_at, subject, status, created_by, updated_at, body)
           VALUES(?,?,?,?,?,?,?)""",
        (KIND, occurred_at, dept["name"], "confirmed", author, now,
         json.dumps(body, ensure_ascii=False)),
    )
    return True, "%s／%s の申し送りを記録しました。" % (dept["name"], occurred_at)


def latest_data_day(conn):
    """既定の対象日は「今日」ではなく「データがある最終日」。

    申し送りは、ダッシュボードに出ている落ち込みに対して書く。
    取り込みは必ず1〜2日遅れるので、今日を既定にすると画面の外を指すことになる。
    """
    row = conn.execute("SELECT MAX(occurred_at) FROM records WHERE kind='売上'").fetchone()
    return (row and row[0]) or datetime.date.today().isoformat()


def recent_notes(conn, limit=10):
    return conn.execute(
        """SELECT occurred_at, subject, created_by, updated_at,
                  json_extract(body,'$.text')     AS text,
                  json_extract(body,'$.category') AS category
             FROM records WHERE kind=? ORDER BY id DESC LIMIT ?""",
        (KIND, limit),
    ).fetchall()


# ---------------------------------------------------------------- 画面

def render_guide_page(cfg, identity):
    """使い方は認証の外に置く ── 鍵を持っていない人が「鍵の貰い方」を読めないと詰む。

    そのかわり、この画面には会社の数字を一切出さない。
    """
    nav = ('<a href="/">ダッシュボード</a><a href="/note">申し送りを書く</a>'
           '<b>使い方</b><a href="/logout">閉じる</a>') if identity else '<a href="/">城を開く</a><b>使い方</b>'
    return string.Template(GUIDE_TEMPLATE.read_text(encoding="utf-8")).substitute(
        theme=theme(),
        company=html.escape(cfg.company), nav=nav)


def render_knowledge_page(conn, cfg, identity, params=None, message="", scope=None):
    """知識の泉。引く側（検索・絞り込み）と、書く側（本質・なぜ・どう使うか）を1枚に置く。"""
    params = params or {}
    query = (params.get("q") or "").strip()
    subject = (params.get("subject") or "").strip()
    type_of = (params.get("type") or "").strip()
    supersedes = (params.get("supersedes") or "").strip()

    def options(items, selected, blank=""):
        out = ['<option value="">%s</option>' % html.escape(blank)] if blank else []
        for item in items:
            out.append('<option value="%s"%s>%s</option>'
                       % (html.escape(item), " selected" if item == selected else "", html.escape(item)))
        return "".join(out)

    departments = [knowledge.COMPANY_WIDE] + [d["name"] for d in cfg.measured()]

    rows = knowledge.search(conn, query, cfg.search_readings) if query else knowledge.active(conn)
    if scope is not None:
        # 全社の知識は誰でも読める。部門の知識は、その部門の人だけ。
        allowed = set(scope) | {knowledge.COMPANY_WIDE}
        rows = [r for r in rows if r["subject"] in allowed]
    if subject:
        rows = [r for r in rows if r["subject"] == subject]
    if type_of:
        rows = [r for r in rows if knowledge.body_of(r).get("type") == type_of]

    edge = (datetime.date.today() - datetime.timedelta(days=knowledge.STALE_DAYS)).isoformat()
    cards = []
    for row in rows:
        body = knowledge.body_of(row)
        reviewed = body.get("reviewed_at", "")
        old = reviewed <= edge
        tags = "".join('<span class="tag">%s</span>' % html.escape(t) for t in body.get("tags", []))
        cards.append(
            '<div class="entry"><div class="ess">%s</div>'
            '<div class="sub">なぜ：<b>%s</b></div><div class="sub">使い方：<b>%s</b></div>'
            '<div class="meta"><span class="tag %s">%s</span><span class="tag">%s</span>%s'
            '<span>記録 %s ／ 見直し <span class="%s">%s</span></span>'
            '<form class="inline" method="post" action="/knowledge">'
            '<input type="hidden" name="action" value="review"><input type="hidden" name="id" value="%d">'
            '<button class="quiet" type="submit">まだ有効</button></form>'
            '<a class="quiet" href="/knowledge?supersedes=%d" '
            'style="text-decoration:none;padding:4px 12px;border:1px solid var(--line);'
            'border-radius:6px;font-size:12px;color:var(--bar)">これを覆す</a>'
            "</div></div>"
            % (html.escape(body.get("essence", "")), html.escape(body.get("why", "")),
               html.escape(body.get("how", "")), html.escape(body.get("type", "")),
               html.escape(body.get("type", "")), html.escape(row["subject"] or ""), tags,
               html.escape(row["created_by"] or ""), "stale" if old else "",
               html.escape(reviewed) + ("（要見直し）" if old else ""), row["id"], row["id"]))

    entries = "".join(cards) or (
        '<div class="none">ありません。%s</div>'
        % ("条件を変えてみてください。" if (query or subject or type_of) else "下の「知識を書く」から最初の1件を。"))

    stale_rows = knowledge.stale(conn)
    stale_block = ""
    if stale_rows:
        # 古い知識が現役の顔をするのを止める。名指ししないと、誰も見直さない。
        stale_block = ('<div class="msg warn"><b>%d件が%d日以上見直されていません。</b>'
                       "まだ有効なら「まだ有効」を、変わったなら「これを覆す」を押してください。"
                       "古い知識が現役の顔をしているのが、いちばん危険です。</div>"
                       % (len(stale_rows), knowledge.STALE_DAYS))

    supersede_field = ""
    if supersedes.isdigit():
        target = conn.execute("SELECT * FROM records WHERE id=? AND kind=?",
                              (int(supersedes), knowledge.KIND)).fetchone()
        if target is not None:
            supersede_field = (
                '<input type="hidden" name="supersedes" value="%d">'
                '<div class="rule">これは <b>#%d「%s」</b> を覆します。'
                "古い方は消えず、履歴として残ります。</div>"
                % (int(supersedes), int(supersedes),
                   html.escape(knowledge.body_of(target).get("essence", ""))))

    author_field = ('<div class="who">%s <span>（アクセスキーで確認済み）</span></div>' % html.escape(identity)
                    if identity else
                    '<input type="text" id="k_author" name="author" maxlength="30" required>')

    nav = ('<a href="/">ダッシュボード</a><a href="/note">申し送りを書く</a>'
           '<b>知識の泉</b><a href="/guide">使い方</a><a href="/logout">閉じる</a>')

    return string.Template(KNOWLEDGE_TEMPLATE.read_text(encoding="utf-8")).substitute(
        theme=theme(),
        company=html.escape(cfg.company), nav=nav, message=message,
        query=html.escape(query),
        subject_options=options(departments, subject, blank="すべての部門"),
        type_options=options(list(knowledge.TYPES), type_of, blank="すべての種類"),
        form_subjects=options(departments, subject or ""),
        form_types=options(list(knowledge.TYPES), ""),
        heading="現役の知識 %d件" % len(rows),
        entries=entries, stale=stale_block,
        supersede_field=supersede_field, author_field=author_field,
        write_open="open" if supersede_field else "")


def render_login_page(cfg, message=""):
    return string.Template(LOGIN_TEMPLATE.read_text(encoding="utf-8")).substitute(
        theme=theme(),
        company=html.escape(cfg.company), message=message)


def render_note_page(instance, conn, cfg, message="", author="", default_day="",
                     identity=None, scope=None):
    def options(items, selected=""):
        return "".join(
            '<option value="%s"%s>%s</option>'
            % (html.escape(i), " selected" if i == selected else "", html.escape(i))
            for i in items)

    rows = recent_notes(conn)
    if scope is not None:
        rows = [r for r in rows if r["subject"] in set(scope)]
    if rows:
        recent = "".join(
            '<div class="note"><div class="meta">%s ／ %s ／ %s ／ %s</div>%s</div>'
            % (html.escape(r["occurred_at"]), html.escape(r["subject"]),
               html.escape(r["category"] or ""), html.escape(r["created_by"]),
               html.escape(r["text"] or ""))
            for r in rows)
    else:
        recent = '<div class="none">まだありません。</div>'

    # 鍵で本人が分かっているなら、名前は入力させない（名乗りではなく、鍵の持ち主を記録する）
    if identity:
        author_field = ('<div class="who">%s <span>（アクセスキーで確認済み）</span></div>'
                        % html.escape(identity))
    else:
        author_field = ('<input type="text" id="author" name="author" value="%s" maxlength="%d" required>'
                        % (html.escape(author), MAX_AUTHOR))

    return string.Template(NOTE_TEMPLATE.read_text(encoding="utf-8")).substitute(
        theme=theme(),
        company=html.escape(cfg.company),
        message=message,
        today=html.escape(default_day or latest_data_day(conn)),
        author_field=author_field,
        departments=options([d["name"] for d in cfg.measured()]),
        categories=options(cfg.note_categories),
        recent=recent,
    )


# ── 回数制限 ──────────────────────────────────────────────
# 鍵の総当たりも、投稿の連打も、画面の作り直しの叩きも、
# 「短い時間に何度も」という同じ形をしている。だから窓で数えて止める。
#
# 記録はメモリだけに置く。プロセスを止めれば消えるが、それでよい ──
# 総当たりは連続した行為なので、途切れた時点で成立しなくなる。
# ここでファイルやDBに書き始めると、止めるための仕組みが新しい重さになる。
LIMITS = {
    "login": (10, 600),    # 鍵の試行：10分に10回
    "write": (30, 60),     # 申し送り・知識の投稿：1分に30回
    "build": (60, 60),     # 画面の作り直し：1分に60回（GETのたびに全部組み直すため）
}
_seen = {}


def too_many(bucket, who):
    limit, window = LIMITS[bucket]
    now = time.monotonic()
    key = (bucket, who)
    recent = [t for t in _seen.get(key, []) if now - t < window]
    recent.append(now)
    _seen[key] = recent
    if len(_seen) > 2000:                       # 溜まりっぱなしにしない
        for dead in [k for k, v in _seen.items() if not v or now - v[-1] > 3600]:
            _seen.pop(dead, None)
    return len(recent) > limit


TOO_MANY_PAGE = (
    "<h1>しばらくお待ちください</h1>"
    "<p>短い時間に何度も送られています。少し待ってからやり直してください。</p>"
    "<p><a href='/'>ダッシュボードへ</a></p>"
)


def make_server(instance, port=8765, host="127.0.0.1"):
    instance = pathlib.Path(instance)

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            print("  %s %s" % (self.command, self.path))

        def _send(self, body, status=200, content_type="text/html; charset=utf-8", cookie=None):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(data)

        def _identity(self):
            """鍵から本人を引く。認証が入っていなければ None（＝名乗りに戻る）。"""
            if not users.enabled(instance):
                return None
            raw = self.headers.get("Cookie") or ""
            jar = http.cookies.SimpleCookie()
            try:
                jar.load(raw)
            except http.cookies.CookieError:
                return None
            morsel = jar.get(COOKIE)
            return users.resolve(instance, morsel.value) if morsel else None

        def _scope(self):
            """この鍵で見てよい部門。None は全社。"""
            name = self._identity()
            return users.scope_of(instance, name) if name else None

        def _needs_login(self):
            return users.enabled(instance) and self._identity() is None

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            conn = db.connect(instance)
            try:
                cfg = config_mod.load(instance)
                if path == "/logout":
                    return self._send(
                        render_login_page(cfg, '<div class="msg">閉じました。</div>'),
                        cookie="%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict" % COOKIE)
                if path == "/guide":
                    return self._send(render_guide_page(cfg, self._identity()))
                if path == "/login" or self._needs_login():
                    return self._send(render_login_page(cfg))
                if path == "/":
                    if too_many("build", self.client_address[0]):
                        return self._send(TOO_MANY_PAGE, 429)
                    # 毎回その場で作り直す。画面は常にいまのDBを映す。
                    build_dashboard.build(instance, nav=True, scope=self._scope())
                    self._send((instance / "out" / "dashboard.html").read_text(encoding="utf-8"))
                elif path == "/note":
                    self._send(render_note_page(instance, conn, cfg, identity=self._identity(),
                                                scope=self._scope()))
                elif path == "/knowledge":
                    params = {k: v[0] for k, v in
                              urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
                    self._send(render_knowledge_page(conn, cfg, self._identity(), params,
                                                    scope=self._scope()))
                else:
                    self._send("<h1>404</h1><p><a href='/'>ダッシュボードへ</a></p>", 404)
            finally:
                conn.close()

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            if path not in ("/note", "/login", "/knowledge"):
                return self._send("<h1>404</h1>", 404)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8", "replace")
            payload = {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

            conn = db.connect(instance)
            try:
                cfg = config_mod.load(instance)

                if path == "/login":
                    # 鍵は総当たりできる形をしている。試行そのものに上限を置く。
                    if too_many("login", self.client_address[0]):
                        return self._send(render_login_page(
                            cfg, '<div class="msg">試行が多すぎます。しばらく待ってからやり直してください。</div>'),
                            429)
                    name = users.resolve(instance, payload.get("token", ""))
                    if name is None:
                        return self._send(render_login_page(
                            cfg, '<div class="msg">このアクセスキーでは開けません。</div>'), 401)
                    return self._send(
                        render_note_page(instance, conn, cfg, identity=name,
                                         message='<div class="msg ok">%s として開きました。</div>'
                                                 % html.escape(name)),
                        cookie="%s=%s; Path=/; HttpOnly; SameSite=Strict" % (COOKIE, payload["token"]))

                if self._needs_login():
                    return self._send(render_login_page(cfg), 401)

                if too_many("write", self.client_address[0]):
                    return self._send(TOO_MANY_PAGE, 429)

                # 鍵で本人が分かっているなら、送られてきた名乗りは捨てる。ここが認証を入れる意味。
                identity = self._identity()
                if identity:
                    payload["author"] = identity

                # 読めない部門には書けない。読みだけ絞って書き込みを開けておくと、
                # 範囲外の部門に他人名義の記録が積める。
                scope = self._scope()
                if scope is not None:
                    target = (payload.get("subject") or "").strip()
                    dept = cfg.resolve(name=target)
                    allowed = set(scope) | {knowledge.COMPANY_WIDE}
                    if dept is None or dept["name"] not in allowed:
                        if not (path == "/knowledge" and target == knowledge.COMPANY_WIDE):
                            return self._send(
                                "<h1>この部門には書けません</h1>"
                                "<p>この鍵で書けるのは %s だけです。</p>"
                                "<p><a href='/'>ダッシュボードへ</a></p>"
                                % html.escape("・".join(sorted(scope))), 403)

                if path == "/knowledge":
                    if payload.get("action") == "review":
                        ok = knowledge.review(conn, int(payload.get("id", 0) or 0), identity or "（記名なし）")
                        conn.commit()
                        banner = ('<div class="msg ok">見直し日を今日に更新しました。</div>' if ok
                                  else '<div class="msg">その知識は見つかりませんでした。</div>')
                        return self._send(render_knowledge_page(conn, cfg, identity, message=banner))
                    result = knowledge.add(conn, cfg, payload)
                    conn.commit()
                    banner = '<div class="msg %s">%s</div>' % ("ok" if result["ok"] else "",
                                                               html.escape(result["message"]))
                    return self._send(
                        render_knowledge_page(conn, cfg, identity,
                                              params={} if result["ok"] else payload, message=banner),
                        200 if result["ok"] else 400)

                ok, message = add_note(conn, cfg, payload)
                conn.commit()
                banner = '<div class="msg %s">%s</div>' % ("ok" if ok else "ng", html.escape(message))
                page = render_note_page(
                    instance, conn, cfg, message=banner, identity=identity,
                    author=payload.get("author", ""),
                    default_day=payload.get("occurred_at", ""))
                self._send(page, 200 if ok else 400)
            finally:
                conn.close()

    server = http.server.ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description="城の薄いローカルサーバ")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1",
                        help="既定は 127.0.0.1。外に開くには利用者の登録が要る")
    parser.add_argument("--expose", action="store_true",
                        help="127.0.0.1 の外に開くことを明示する（HTTPSではない点を承知のうえで）")
    parser.add_argument("--instance")
    args = parser.parse_args()

    instance = db.instance_dir(args.instance)
    refusal = guard_exposure(instance, args.host, args.expose)
    if refusal:
        raise SystemExit(refusal)
    server = make_server(instance, args.port, args.host)
    url = "http://127.0.0.1:%d" % server.server_address[1]
    print("城を開きました: %s" % url)
    print("  %s/      ダッシュボード（開くたびに作り直します）" % url)
    print("  %s/note  申し送り" % url)
    if users.enabled(instance):
        print("  認証: 有効（登録 %d人）" % len(users.load(instance)))
    else:
        print("  認証: 無効 ── users.json がありません。127.0.0.1 に閉じていることだけが防御です。")
    if args.host not in LOCAL_HOSTS:
        print("  ※ HTTPSではありません。社内網でも平文で流れます。外に出すならプロキシでHTTPSを終端してください。")
    print("止めるには Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n止めました。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
