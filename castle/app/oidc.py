"""本人確認を、外に出す。

城はアカウントを持たない。**誰かを決めるのは Google Workspace、何が見えるかを決めるのが城。**
入社・退職・異動は情シスがGoogle側で済ませ、城の名簿は触らなくてよい ──
Googleでアカウントを止めれば、そもそも城の入口に届かないため。

ここにあるのは、Googleが渡してくるIDトークン（JWT）を**信じる前に確かめる**部分。
確かめるのは7つ。1つでも欠けたら、そこが穴になる。

  1. 署名が、発行者の鍵で作られたものか（RS256）
  2. その鍵が、発行者が「これで署名した」と指した鍵か（kid）
  3. 発行者（iss）が、こちらの知っている発行者か
  4. 宛先（aud）が、このアプリ向けか ── 別アプリ向けのトークンを流用させない
  5. 期限（exp）が切れていないか
  6. 投げ返しの合言葉（nonce）が、こちらが出したものか ── 使い回しを防ぐ
  7. 会社のドメイン（hd）か ── 個人のGoogleアカウントを弾く

**外部ライブラリは使わない。** RS256の検証は、要するに「署名を公開鍵で開いて、
中のハッシュと突き合わせる」だけで、必要なのは巨大整数のべき乗剰余（pow）と
SHA-256 と base64 ── どれも標準ライブラリにある。

**実テナントとの疎通は、ここでは確かめていない。** 判定の中に発行者を立てて
署名と検証を突き合わせているだけなので、Google本番に繋ぐときは別途確かめること。
"""

import base64
import datetime
import hashlib
import json
import pathlib
import urllib.parse
import urllib.request
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import users

# SHA-256 の DigestInfo（PKCS#1 v1.5）。署名を開いた中身は、この並びで終わる。
SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")

# Google の発行者と鍵の在り処。設定で差し替えられるようにしてある
# ── Microsoft へ寄せ直すときに、ここだけ変えれば済む形にするため。
GOOGLE = {
    "issuer": "https://accounts.google.com",
    "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
    "discovery": "https://accounts.google.com/.well-known/openid-configuration",
}


class Rejected(Exception):
    """信じなかった理由。**理由を残さない拒否は、原因が追えない。**"""


def configured(cfg):
    """Googleの口を開いてよいか。**設定が埋まっていなければ開かない。**

    半端な設定で開くと「押しても入れないボタン」になる。押せるなら入れる状態にする。
    """
    auth = cfg.auth or {}
    return bool(auth.get("client_id")) and bool(auth.get("auth_endpoint"))


def start_url(cfg, redirect_uri, state, nonce, challenge):
    """Googleへ送り出すURL。**PKCEを付ける** ── 受けの途中で横取りされても使わせない。"""
    auth = cfg.auth or {}
    params = {
        "client_id": auth["client_id"], "redirect_uri": redirect_uri,
        "response_type": "code", "scope": "openid email profile",
        "state": state, "nonce": nonce,
        "code_challenge": challenge, "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    if auth.get("domain"):
        # 会社のドメインのアカウントを既定で選ばせる（検証は受けの側でも必ずやる）
        params["hd"] = auth["domain"]
    return auth["auth_endpoint"] + "?" + urllib.parse.urlencode(params)


def challenge_of(verifier):
    return base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def exchange(cfg, code, redirect_uri, verifier, secret, opener=None):
    """受け取った引換券をIDトークンに換える。**ここだけが外へ出る通信。**"""
    auth = cfg.auth or {}
    data = urllib.parse.urlencode({
        "code": code, "client_id": auth["client_id"], "client_secret": secret,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code",
        "code_verifier": verifier}).encode("ascii")
    request = urllib.request.Request(
        auth["token_endpoint"], data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with (opener or urllib.request.urlopen)(request, timeout=15) as response:
        got = json.loads(response.read().decode("utf-8"))
    if "id_token" not in got:
        raise Rejected("引換に失敗しました: %s" % got.get("error", "id_token がありません"))
    return got["id_token"]


def fetch_jwks(cfg, opener=None):
    auth = cfg.auth or {}
    with (opener or urllib.request.urlopen)(auth["jwks_uri"], timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def b64url(data):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _int(data):
    return int.from_bytes(b64url(data), "big")


def _rsa_ok(message, signature, modulus, exponent):
    """RS256。署名を公開鍵で開いて、PKCS#1 v1.5 の並びとハッシュを突き合わせる。

    開いた中身は  00 01 FF..FF 00 <DigestInfo> <SHA-256>  になっているはず。
    **前置きの形まで見る。** ハッシュ部分だけ比べると、詰め物を細工した署名が通る。
    """
    size = (modulus.bit_length() + 7) // 8
    if len(signature) != size:
        return False
    opened = pow(int.from_bytes(signature, "big"), exponent, modulus).to_bytes(size, "big")
    want = (b"\x00\x01" + b"\xff" * (size - len(SHA256_PREFIX) - 32 - 3)
            + b"\x00" + SHA256_PREFIX + hashlib.sha256(message).digest())
    return len(want) == size and _same(opened, want)


def _same(a, b):
    """比べる時間で中身が漏れないようにする。"""
    if len(a) != len(b):
        return False
    diff = 0
    for x, y in zip(a, b):
        diff |= x ^ y
    return diff == 0


def verify(token, jwks, want, now=None):
    """IDトークンを確かめて、中身を返す。信じられなければ Rejected を投げる。"""
    now = now or int(datetime.datetime.now().timestamp())
    parts = (token or "").split(".")
    if len(parts) != 3:
        raise Rejected("形が違います（3つに分かれていません）")
    head_raw, body_raw, sig_raw = parts

    try:
        head = json.loads(b64url(head_raw))
        claims = json.loads(b64url(body_raw))
        signature = b64url(sig_raw)
    except Exception as broken:                       # noqa: BLE001
        raise Rejected("読めません: %s" % broken) from None

    if head.get("alg") != "RS256":
        # alg=none や HS256 への差し替えは、古典的な抜け道。**こちらが決める。**
        raise Rejected("署名の方式が RS256 ではありません: %r" % head.get("alg"))

    # 発行者が「これで署名した」と指した鍵だけを使う。
    # kid を見ずに総当たりすると「どれかで通れば通る」になり、鍵の入れ替えが意味を失う。
    kid = head.get("kid")
    key = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
    if key is None:
        raise Rejected("署名に使われた鍵が見つかりません（kid=%r）" % kid)

    signed = ("%s.%s" % (head_raw, body_raw)).encode("ascii")
    if not _rsa_ok(signed, signature, _int(key["n"]), _int(key["e"])):
        raise Rejected("署名が合いません（中身が書き換えられているか、鍵が違います）")

    if claims.get("iss") != want["issuer"]:
        raise Rejected("発行者が違います: %r" % claims.get("iss"))

    aud = claims.get("aud")
    aud = aud if isinstance(aud, list) else [aud]
    if want["audience"] not in aud:
        raise Rejected("このアプリ宛てではありません: %r" % aud)

    if int(claims.get("exp", 0)) <= now:
        raise Rejected("期限が切れています")
    if int(claims.get("iat", 0)) > now + 300:
        raise Rejected("発行時刻が先すぎます")

    if want.get("nonce") and claims.get("nonce") != want["nonce"]:
        raise Rejected("投げ返しの合言葉が合いません")

    # 会社のドメインか。個人のGoogleアカウントは hd を持たない。
    if want.get("domain") and claims.get("hd") != want["domain"]:
        raise Rejected("会社のドメインではありません: %r" % claims.get("hd"))

    if not claims.get("email"):
        raise Rejected("メールアドレスがありません")
    if claims.get("email_verified") is False:
        raise Rejected("メールアドレスが確認されていません")
    return claims


def sign_in(instance, token, jwks, want, now=None):
    """署名を確かめ、名簿を引く。**両方そろって初めて入れる。**

    署名が正しいのは「その人が本物である」ことしか言わない。
    **本物であることと、見てよいことは別。** ドメインの中の全員を通したら、認可が消える。
    """
    claims = verify(token, jwks, want, now=now)
    row = users.identify(instance, claims["email"])
    if row is None:
        return None
    return {"email": claims["email"], "name": row.get("name") or claims.get("name") or claims["email"],
            "scope": row.get("scope") or None}


# ── 判定のための発行者 ──────────────────────────────────────
#
# 実テナントが無くても、署名を作って壊して確かめられるようにする。
# **通る側1つに対して、弾く側を確かめるための道具。** 本番の経路では使わない。

def _probable_prime(bits, rng):
    while True:
        n = rng.getrandbits(bits) | (1 << (bits - 1)) | 1
        if _is_prime(n):
            return n


def _is_prime(n, rounds=24):
    small = (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37)
    if n < 2:
        return False
    for p in small:
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d, r = d // 2, r + 1
    for a in small[:rounds]:
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = x * x % n
            if x == n - 1:
                break
        else:
            return False
    return True


class TestIssuer:
    """判定のなかの発行者。鍵はその場で作る ── **秘密鍵をリポジトリに置かない。**"""

    def __init__(self, issuer, audience, bits=1024, seed=20260901):
        import random
        rng = random.Random(seed)
        self.issuer, self.audience = issuer, audience
        self.now = int(datetime.datetime(2026, 9, 1, 9, 0).timestamp())
        self.keys = {}
        for kid in ("k-main", "k-other"):
            p = _probable_prime(bits // 2, rng)
            q = _probable_prime(bits // 2, rng)
            while q == p:
                q = _probable_prime(bits // 2, rng)
            n, e = p * q, 65537
            d = pow(e, -1, (p - 1) * (q - 1))
            self.keys[kid] = {"n": n, "e": e, "d": d}

    @staticmethod
    def _b64(raw):
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    def jwks(self):
        def num(v):
            return self._b64(v.to_bytes((v.bit_length() + 7) // 8, "big"))
        return {"keys": [{"kid": kid, "kty": "RSA", "alg": "RS256", "use": "sig",
                          "n": num(k["n"]), "e": num(k["e"])}
                         for kid, k in self.keys.items()]}

    def _sign(self, kid, signed):
        key = self.keys[kid]
        size = (key["n"].bit_length() + 7) // 8
        block = (b"\x00\x01" + b"\xff" * (size - len(SHA256_PREFIX) - 32 - 3)
                 + b"\x00" + SHA256_PREFIX + hashlib.sha256(signed).digest())
        sig = pow(int.from_bytes(block, "big"), key["d"], key["n"])
        return self._b64(sig.to_bytes(size, "big"))

    def token(self, email, hd, nonce, lifetime=3600, other_key=False):
        kid = "k-other" if other_key else "k-main"
        head = self._b64(json.dumps({"alg": "RS256", "kid": kid, "typ": "JWT"}).encode())
        claims = {"iss": self.issuer, "aud": self.audience, "sub": "1234567890",
                  "email": email, "email_verified": True, "nonce": nonce,
                  "iat": self.now, "exp": self.now + lifetime}
        if hd:
            claims["hd"] = hd
        body = self._b64(json.dumps(claims).encode())
        # 「発行者が指した鍵」は、常に本物の側を指させる ── 鍵の取り違えを試すため
        if other_key:
            head = self._b64(json.dumps({"alg": "RS256", "kid": "k-main", "typ": "JWT"}).encode())
        return "%s.%s.%s" % (head, body, self._sign(kid, ("%s.%s" % (head, body)).encode()))

    @staticmethod
    def tampered(token, before, after):
        """署名はそのままに、中身だけ書き換える。**署名を見ていなければ通ってしまう。**"""
        head, body, sig = token.split(".")
        claims = json.loads(b64url(body))
        for key, value in before.items():
            if claims.get(key) != value:
                raise ValueError("書き換える前提が違います: %s" % key)
        claims.update(after)
        swapped = base64.urlsafe_b64encode(
            json.dumps(claims).encode()).rstrip(b"=").decode("ascii")
        return "%s.%s.%s" % (head, swapped, sig)
