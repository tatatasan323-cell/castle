"""部門マスタ。「誰も間を担当していない」の、担当する側。

販売管理は「加工食品部」、勤怠は「加工食品」、物流の実績は「物流ｾﾝﾀｰ」。
どのベンダーも間違っていない。突き合わせる側が対応表を持つしかない。
その対応表が instance/config.json であり、城が横串たりうる根拠でもある。
"""

import json
import re
import unicodedata

from normalize import norm

# 会計は「1010 加工食品」とコードと名前を1セルに入れてくる。分けてから引く。
_CODE_NAME = re.compile(r"^(\d{3,6})[\s　]+(.+)$")


class Config:
    def __init__(self, path):
        self.path = path
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.company = raw["company"]
        self.metric = raw["metric"]
        self.departments = raw["departments"]
        self.note_categories = raw.get("note_categories", [])
        self.search_readings = raw.get("search_readings", {})
        self.budget = raw.get("budget", {})
        self.accounting = raw.get("accounting", {})
        self.fiscal = raw.get("fiscal", {"start_month": 4})
        self.backup = raw.get("backup", {})

        self._lookup = {}
        for dept in self.departments:
            keys = [dept["code"], dept["name"], *dept.get("aliases", [])]
            for key in keys:
                self._lookup[norm(key)] = dept

    def resolve(self, code="", name=""):
        """コード優先、無ければ名称・別名で引く。引けなければ None（＝黙って捨てない）。"""
        for key in (code, name):
            dept = self._lookup.get(norm(key))
            if dept:
                return dept
        for key in (code, name):
            match = _CODE_NAME.match(unicodedata.normalize("NFKC", (key or "").strip()))
            if match:
                dept = self._lookup.get(norm(match.group(1))) or self._lookup.get(norm(match.group(2)))
                if dept:
                    return dept
        return None

    def measured(self):
        """人時生産性を出す部門。売上を持たない間接部門はここに入らない。"""
        return [d for d in self.departments if d.get("productivity")]


def load(instance):
    return Config(instance / "config.json")
