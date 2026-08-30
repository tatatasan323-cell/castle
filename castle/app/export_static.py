"""公開用に、画面を全部まとめて書き出す。

GitHub Pages に置けるのはHTMLだけで、サーバは動かない。
だからといってダッシュボード1枚だけを出すと、**申し送りと知識の泉という
この城の半分が、公開されたものからは存在しないことになる。**

読むだけの形で全画面を出し、**書けないことは画面に書く。**
黙って動かないボタンを置くほうが、無いより悪い。
"""

import argparse
import html
import pathlib
import re
import sqlite3
import sys

APP = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(APP))

import build_dashboard                      # noqa: E402
import config as config_mod                 # noqa: E402
import db                                   # noqa: E402
import serve                                # noqa: E402

# サーバのパスから、静的ファイル名へ
LINKS = {'href="/"': 'href="index.html"',
         'href="/note"': 'href="note.html"',
         'href="/knowledge"': 'href="knowledge.html"',
         'href="/guide"': 'href="guide.html"'}

NOTICE = (
    '<div class="notice"><b>これは読むだけの見本です。</b>'
    'サーバが動いていないので、書き込みはできません（入力欄は押せなくしてあります）。'
    '実際に動かすと、ここから申し送りと知識が書けます ── '
    '<a href="https://github.com/tatatasan323-cell/castle">リポジトリ</a>を取得して '
    '<code>python castle/app/serve.py</code> で開きます。</div>')

FORM_FIELDS = re.compile(r"<(input|select|textarea|button)\b")


def flatten(page, read_only):
    """リンクを静的ファイルへ差し替え、書き込みの口を閉じる。"""
    for old, new in LINKS.items():
        page = page.replace(old, new)
    # 閉じるボタンは静的版に意味がない。残すと「押しても何も起きない」になる。
    page = re.sub(r'<a href="/logout">[^<]*</a>', "", page)
    # 覆す・絞り込むのリンクはサーバが要る。押しても何も起きないものは外す。
    page = re.sub(r'<a [^>]*href="/[^"]*"[^>]*>.*?</a>', "", page, flags=re.S)
    if not read_only:
        return page
    page = FORM_FIELDS.sub(lambda m: "<%s disabled" % m.group(1), page)
    # 送り先も外す。閉じた入力欄の下に生きた action が残っていると、
    # 「押せば送れるのでは」と読めてしまう。
    page = re.sub(' action="/[^"]*"', '', page)
    # 見出しの直後に、書けない理由を置く。h1 は全画面にある。
    return re.sub(r"</h1>", lambda m: m.group(0) + NOTICE, page, count=1)


def main():
    parser = argparse.ArgumentParser(description="公開用の静的な一式を書き出す")
    parser.add_argument("--instance")
    parser.add_argument("--out", default="docs")
    args = parser.parse_args()

    instance = db.instance_dir(args.instance)
    out = pathlib.Path(args.out)
    if not out.is_absolute():
        out = instance.parent / out
    out.mkdir(parents=True, exist_ok=True)

    cfg = config_mod.load(instance)
    conn = sqlite3.connect(instance / "data.db")
    conn.row_factory = sqlite3.Row
    try:
        build_dashboard.build(instance, nav=True)
        pages = {
            "index.html": ((instance / "out" / "dashboard.html").read_text(encoding="utf-8"), False),
            "note.html": (serve.render_note_page(instance, conn, cfg), True),
            "knowledge.html": (serve.render_knowledge_page(conn, cfg, None), True),
            "guide.html": (serve.render_guide_page(cfg, None), False),
        }
        for name, (page, read_only) in pages.items():
            (out / name).write_text(flatten(page, read_only), encoding="utf-8")
            print("  %-16s %6.1f KB" % (name, len(page) / 1024))
    finally:
        conn.close()
    print("書き出しました: %s" % out)


if __name__ == "__main__":
    main()
