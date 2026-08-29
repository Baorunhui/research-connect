"""Self-citation detection — strict but reasonable.

Strategy:
1. Exact normalized name match (highest confidence)
2. Surname + first name initial match (handles abbreviations like "X. Yang" vs "Xue Yang")
3. Chinese name match (姓 + 名首字 or full Chinese name)

NOT just surname-only — that would false-positive all "Wang"s.
"""
from typing import List, Optional, Set, Tuple
import re


def _parse_name(name: str) -> dict:
    """Parse a name into structured components.

    Returns: {
        "full": normalized full name,
        "surname": last name (Western) or first char (Chinese),
        "given_initial": first letter of given name,
        "given_full": full given name,
        "is_cjk": bool,
        "chinese_chars": set of Chinese characters in name,
    }
    """
    name = name.strip()
    if not name:
        return {"full": "", "surname": "", "given_initial": "", "given_full": "",
                "is_cjk": False, "chinese_chars": set()}

    # Extract Chinese characters
    chinese = set(c for c in name if '\u4e00' <= c <= '\u9fff')
    is_cjk = len(chinese) >= 2

    # Clean parenthetical content
    clean = re.sub(r'[（(].*?[）)]', '', name).strip()

    if is_cjk:
        # Chinese: first char = surname, rest = given name
        chars = [c for c in clean if '\u4e00' <= c <= '\u9fff']
        surname = chars[0] if chars else ""
        given = "".join(chars[1:]) if len(chars) > 1 else ""
        # Also extract any Latin parts (e.g., "杨雪 Xue Yang")
        latin_parts = re.findall(r'[a-zA-Z]{2,}', name)
        latin_surname = latin_parts[-1].lower() if latin_parts else ""
        latin_initial = latin_parts[0][0].lower() if latin_parts else ""
        return {
            "full": "".join(chars),
            "surname": surname,
            "given_initial": given[0] if given else "",
            "given_full": given,
            "is_cjk": True,
            "chinese_chars": chinese,
            "latin_surname": latin_surname,
            "latin_initial": latin_initial,
        }

    # Western name handling
    # Handle "Last, First" format
    if ',' in clean:
        parts = clean.split(',', 1)
        surname = parts[0].strip().split()[-1]
        given_parts = parts[1].strip().split()
    else:
        parts = clean.split()
        if not parts:
            return {"full": "", "surname": "", "given_initial": "", "given_full": "",
                    "is_cjk": False, "chinese_chars": set()}
        surname = parts[-1]  # Last word = surname
        given_parts = parts[:-1]

    # Given name: skip single-letter initials for "full" but keep initial
    given_initial = ""
    given_full = ""
    for p in given_parts:
        p_clean = p.strip('.').strip()
        if p_clean:
            if not given_initial:
                given_initial = p_clean[0].lower()
            if len(p_clean) > 1:
                given_full = p_clean.lower()
                break

    return {
        "full": re.sub(r'[^\w\s]', '', clean.lower()),
        "surname": surname.lower().strip('.'),
        "given_initial": given_initial,
        "given_full": given_full,
        "is_cjk": False,
        "chinese_chars": chinese,
    }


class SelfCitationDetector:
    """Detect self-citations with surname + initial matching.

    Strict enough to catch abbreviations (X. Yang = Xue Yang),
    but not so strict as to false-positive all same-surname authors.
    """

    def check(self, target_authors: List[dict], citing_authors: List[dict]) -> dict:
        if not target_authors or not citing_authors:
            return {"is_self_citation": False, "method": "none", "matched_pair": None}

        target_parsed = [_parse_name(a.get("name", "")) for a in target_authors]
        citing_parsed = [_parse_name(a.get("name", "")) for a in citing_authors]

        # Step 1: Exact full name match (normalize: sorted words, ignore order)
        def _norm(full):
            return " ".join(sorted(full.split()))
        target_norms = {_norm(p["full"]) for p in target_parsed if p["full"]}
        for cp in citing_parsed:
            if cp["full"] and _norm(cp["full"]) in target_norms:
                return {"is_self_citation": True, "method": "exact",
                        "matched_pair": (cp["full"], cp["full"])}

        # Very common surnames — require full given name match (initial only is too loose)
        _COMMON = {
            'wang', 'li', 'zhang', 'liu', 'chen', 'yang', 'huang', 'zhao',
            'wu', 'zhou', 'xu', 'sun', 'ma', 'zhu', 'hu', 'guo', 'he',
            'lin', 'luo', 'zheng', 'liang', 'xie', 'tang', 'wei', 'feng',
            'deng', 'cao', 'yuan', 'lu', 'yu', 'yan',
        }

        # Step 2: Surname + given name matching
        # Common surnames (Li/Wang/Zhang/Yang/Yu/Zhou): require full given name
        # Rare surnames (Da/Qingyun): initial is enough
        for tp in target_parsed:
            if not tp["surname"]:
                continue
            for cp in citing_parsed:
                if not cp["surname"]:
                    continue

                matched = False
                is_common = tp["surname"] in _COMMON

                # Western name matching
                if not tp["is_cjk"] and not cp["is_cjk"]:
                    if tp["surname"] == cp["surname"]:
                        if is_common:
                            # Common surname: need full given name match
                            if (tp["given_full"] and cp["given_full"] and
                                    tp["given_full"] == cp["given_full"]):
                                matched = True
                            # Or initial match + one has abbreviated (X. Yang vs Xue Yang)
                            elif (tp["given_initial"] and cp["given_initial"] and
                                  tp["given_initial"] == cp["given_initial"] and
                                  (not tp["given_full"] or not cp["given_full"])):
                                matched = True  # One side is abbreviated
                        else:
                            # Rare surname: initial is enough
                            if (tp["given_initial"] and cp["given_initial"] and
                                    tp["given_initial"] == cp["given_initial"]):
                                matched = True

                # Chinese name matching
                elif tp["is_cjk"] and cp["is_cjk"]:
                    # Same Chinese surname + same first char of given name
                    if (tp["surname"] == cp["surname"] and
                            tp["given_initial"] and cp["given_initial"] and
                            tp["given_initial"] == cp["given_initial"]):
                        matched = True
                    # Full Chinese name match (2+ chars)
                    if tp["chinese_chars"] and len(tp["chinese_chars"] & cp["chinese_chars"]) >= 2:
                        matched = True

                # Cross-language: Chinese target vs Western citing (or vice versa)
                elif tp["is_cjk"] and not cp["is_cjk"]:
                    # Compare Chinese author's latin_surname with Western surname
                    latin_s = tp.get("latin_surname", "")
                    latin_i = tp.get("latin_initial", "")
                    if (latin_s and latin_s == cp["surname"] and
                            latin_i and cp["given_initial"] and
                            latin_i == cp["given_initial"]):
                        matched = True
                elif not tp["is_cjk"] and cp["is_cjk"]:
                    latin_s = cp.get("latin_surname", "")
                    latin_i = cp.get("latin_initial", "")
                    if (latin_s and latin_s == tp["surname"] and
                            latin_i and tp["given_initial"] and
                            latin_i == tp["given_initial"]):
                        matched = True

                if matched:
                    t_name = target_authors[target_parsed.index(tp)].get("name", "")
                    c_name = citing_authors[citing_parsed.index(cp)].get("name", "")
                    return {"is_self_citation": True, "method": "surname_initial",
                            "matched_pair": (t_name, c_name)}

        return {"is_self_citation": False, "method": "none", "matched_pair": None}
