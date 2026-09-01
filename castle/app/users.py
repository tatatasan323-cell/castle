"""利用者と、その鍵。パスワードは作らせない。

  python castle/app/users.py --add    "農産部長 田中"
  python castle/app/users.py --list
  python castle/app/users.py --revoke "農産部長 田中"

**パスワード認証を自作しない。** 人にパスワードを作らせると、使い回しと弱い文字列が必ず入る。
代わりに十分に長いランダムな鍵をこちらが発行し、本人はそれを一度貼るだけにする。
強度チェックもリセット手続きも要らず、失効は1行消すだけで済む。

保管するのは **ハッシュだけ**。平文の鍵は発行時に一度しか表示しない。
控え損ねたら再発行する（同じ名前で発行し直すと、古い鍵はその場で失効する）。

users.json が **存在すること** が認証の入り切りスイッチ。
無ければ認証なし（127.0.0.1 限定の一人使い）。有れば中身が0人でも全員が入れない ── 開いている側に倒さない。
"""

import argparse
import datetime
import hashlib
import hmac
import json
import pathlib
import secrets
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import config as config_mod
import db

FILE = "users.json"


def path_of(instance):
    return pathlib.Path(instance) / FILE


def enabled(instance):
    """認証を要求するかどうか。ファイルの有無だけで決める。"""
    return path_of(instance).exists()


def _digest(token):
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def load(instance):
    path = path_of(instance)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("users", [])


def save(instance, rows):
    path_of(instance).write_text(
        json.dumps({"users": rows}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _norm(email):
    """メールアドレスは大文字小文字を区別しない。**同じ人を2人にしない。**"""
    return (email or "").strip().lower()


def enroll(instance, email, name, scope=None):
    """名簿に載せる。**鍵は発行しない。** 本人確認は Google Workspace が済ませる。

    城が持つのは「このメールアドレスは、どの部門まで見てよいか」だけ。
    入社・退職・異動は情シスがGoogle側で済ませ、城の名簿はそのまま置いておける
    ── Googleでアカウントを止めれば、そもそも城の入口に届かないため。
    """
    email = _norm(email)
    if "@" not in email:
        raise ValueError("メールアドレスが要ります: %r" % email)
    rows = [r for r in load(instance) if _norm(r.get("email")) != email]
    row = {"email": email, "name": (name or "").strip() or email,
           "enrolled": datetime.datetime.now().isoformat(timespec="seconds")}
    if scope:
        row["scope"] = sorted(set(scope))
    rows.append(row)
    save(instance, sorted(rows, key=lambda r: r.get("email", "")))
    return row


def identify(instance, who):
    """名簿を引く。**載っていなければ None。**

    ここが認可の要。Googleで本人確認が通っても、名簿に無ければ城には入れない。
    「同じ会社のドメインなら通す」にすると、全社員が経営の数字を見られる
    ── それは認可が無いのと同じ。

    メールアドレスでも表示名でも引ける。IdPを繋ぐ前に配った鍵の利用者は
    メールアドレスを持っていないので、そちらも通す（移行のあいだだけ）。
    """
    key = _norm(who)
    for row in load(instance):
        if _norm(row.get("email")) == key or row.get("name") == who:
            return row
    return None


def issue(instance, name, scope=None):
    """鍵を発行して平文を返す。同じ名前で出し直すと、古い鍵は失効する。

    scope に部門名の一覧を渡すと、その鍵ではその部門ぶんしか見えない。
    省略は全社 ── **既定を絞らないのは、既にある鍵を壊さないため**。
    絞りたい鍵は、絞ると明示して発行する。
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("表示名が空です。")
    token = secrets.token_urlsafe(32)
    rows = [r for r in load(instance) if r["name"] != name]
    row = {"name": name, "hash": _digest(token),
           "issued": datetime.datetime.now().isoformat(timespec="seconds")}
    if scope:
        row["scope"] = sorted(set(scope))
    rows.append(row)
    save(instance, sorted(rows, key=lambda r: r["name"]))
    return token


def resolve(instance, token):
    """鍵から本人を引く。引けなければ None。"""
    if not token:
        return None
    digest = _digest(token)
    for row in load(instance):
        if hmac.compare_digest(row["hash"], digest):
            return row["name"]
    return None


def scope_of(instance, who):
    """その人が見てよい部門。None は全社。メールアドレスでも表示名でも引ける。

    scope が書かれていなければ全社 ── 絞るのは、絞ると書いた行だけ。
    **「名簿に無い人」と「範囲を絞っていない人」は別物。** 前者は identify() が弾く。
    """
    key = _norm(who)
    for row in load(instance):
        if _norm(row.get("email")) == key or row.get("name") == who:
            value = row.get("scope")
            return list(value) if value else None
    return None


def revoke(instance, who):
    """名簿から外す。メールアドレスでも表示名でも外せる。"""
    rows = load(instance)
    key = _norm(who)
    kept = [r for r in rows
            if _norm(r.get("email")) != key and r.get("name") != (who or "").strip()]
    if len(kept) == len(rows):
        return False
    save(instance, kept)
    return True


def main():
    parser = argparse.ArgumentParser(description="城の利用者と鍵を管理する")
    parser.add_argument("--instance")
    parser.add_argument("--add", metavar="表示名", help="鍵を発行する（平文はこの時だけ表示）")
    parser.add_argument("--scope", metavar="部門名",
                        help="その鍵で見てよい部門（カンマ区切り）。省略は全社")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--revoke", metavar="表示名")
    args = parser.parse_args()
    instance = db.instance_dir(args.instance)

    if args.add:
        limit = [x.strip() for x in (args.scope or "").split(",") if x.strip()]
        if limit:
            cfg = config_mod.load(instance)
            unknown = [x for x in limit if cfg.resolve(name=x) is None]
            if unknown:
                raise SystemExit("対応表にない部門です: %s" % "、".join(unknown))
            limit = [cfg.resolve(name=x)["name"] for x in limit]
        token = issue(instance, args.add, limit)
        print("\n%s のアクセスキーを発行しました（%s）。\n"
              % (args.add, "・".join(limit) + " のみ" if limit else "全社"))
        print("    %s\n" % token)
        print("この画面にしか出ません。本人に渡してください（渡す経路も、鍵と同じ扱いで）。")
        print("控え損ねたら、同じ名前で --add し直してください（古い鍵は失効します）。")
    elif args.revoke:
        print("失効させました: %s" % args.revoke if revoke(instance, args.revoke)
              else "見つかりません: %s" % args.revoke)
    elif args.list or True:
        rows = load(instance)
        if not enabled(instance):
            print("users.json がありません ── 認証は無効です（127.0.0.1 限定で使う前提）。")
            return
        print("登録 %d人（%s）" % (len(rows), path_of(instance)))
        for row in rows:
            print("  %-24s 発行 %s  範囲 %s"
                  % (row["name"], row["issued"][:16],
                     "・".join(row["scope"]) if row.get("scope") else "全社"))
        if not rows:
            print("  （0人。users.json があるので、いまは誰も入れません）")


if __name__ == "__main__":
    main()
