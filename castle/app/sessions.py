"""入ったあとの覚え。**クッキーに鍵そのものを載せない。**

これまでは、配ったアクセスキーをそのままクッキーに入れていた。鍵の文字列が
ブラウザにも通信路にも残り、盗られたら失効させるまで使われ続ける。

代わりに「誰か」と「いつまでか」を書いて、城の署名を添えたものを渡す。

    <メールアドレス>|<期限>|<署名>

署名は城しか作れないので、中身を書き換えると読めなくなる。期限も中に書いてあるので、
**放っておいても切れる** ── 退職者のセッションが永久に生き残らない。

Google 側でアカウントを止めれば新しくは入れないが、**発行済みのセッションはその瞬間には切れない**。
だから期限を短くする（既定8時間＝1営業日）。ここは、締め出しの速さと使い勝手の釣り合いの問題。
"""

import base64
import datetime
import hashlib
import hmac
import pathlib
import secrets
import urllib.parse

KEY_FILE = "session.key"
DEFAULT_HOURS = 8


def _secret(instance):
    """城の署名鍵。無ければ作る。**リポジトリには入れない**（.gitignore 済み）。"""
    path = pathlib.Path(instance) / KEY_FILE
    if not path.exists():
        path.write_text(secrets.token_urlsafe(48), encoding="utf-8")
    return path.read_text(encoding="utf-8").strip().encode("utf-8")


def _sign(instance, body):
    return base64.urlsafe_b64encode(
        hmac.new(_secret(instance), body.encode("utf-8"), hashlib.sha256).digest()
    ).rstrip(b"=").decode("ascii")


def issue(instance, who, hours=DEFAULT_HOURS, now=None):
    now = now or datetime.datetime.now()
    until = int((now + datetime.timedelta(hours=hours)).timestamp())
    # クッキーはASCIIしか載らない。表示名は日本語のことがあるので、必ず符号化する
    # ── 2026-09-02、日本語の名前を入れてヘッダの書き出しが落ちた。
    body = "%s|%d" % (urllib.parse.quote(who, safe=""), until)
    return "%s|%s" % (body, _sign(instance, body))


def read(instance, value, now=None):
    """クッキーから本人を引く。信じられなければ None。"""
    now = int((now or datetime.datetime.now()).timestamp())
    parts = (value or "").rsplit("|", 2)
    if len(parts) != 3:
        return None
    who, until, signature = parts
    if not hmac.compare_digest(_sign(instance, "%s|%s" % (who, until)), signature):
        return None
    try:
        if int(until) <= now:
            return None
    except ValueError:
        return None
    return urllib.parse.unquote(who) or None
