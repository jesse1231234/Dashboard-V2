# services/canvas.py
from __future__ import annotations

from typing import Optional, List, Dict
import re

import httpx
import pandas as pd
from bs4 import BeautifulSoup


class CanvasService:
    """
    Read-only Canvas API client used to derive:
      - Module order and items
      - Echo video titles per module (from ExternalTool/ExternalUrl item titles OR page-embed iframe titles)
      - Active student count (preferred # Students KPI)

    Auth: Personal Access Token (Authorization: Bearer <token>)
    """

    def __init__(self, base_url: str, token: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    # ---------------- Internal helpers ----------------

    def _get_all(self, url: str, params: Dict | None = None) -> List[Dict]:
        out: List[Dict] = []
        next_url = url
        next_params = params or {}

        while next_url:
            r = self.client.get(next_url, params=next_params)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                out.extend(data)
            elif isinstance(data, dict):
                out.append(data)

            # Parse Link header for rel="next"
            next_url = None
            link = r.headers.get("Link")
            if link:
                for part in (p.strip() for p in link.split(",")):
                    if 'rel="next"' in part:
                        next_url = part.split(";")[0].strip().strip("<>").strip()
                        break

            # only pass params on first request
            next_params = None

        return out

    # Title cleanup patterns: (hh:mm[:ss]), "(read only)", "- 12345"
    _DUR_TAIL_RE   = re.compile(r"\s*\((?:\d{1,2}:)?\d{1,2}:\d{2}\)\s*$", re.I)
    _READONLY_RE   = re.compile(r"\s*\(read only\)\s*$", re.I)
    _NUM_ID_TAIL_RE = re.compile(r"\s*-\s*\d{4,}\s*$")

    @classmethod
    def _strip_noise(cls, title: str) -> str:
        if not title:
            return ""
        t = str(title).strip()
        t = cls._READONLY_RE.sub("", t)
        t = cls._DUR_TAIL_RE.sub("", t)
        t = cls._NUM_ID_TAIL_RE.sub("", t)
        return t.strip()

    # ---------------- Public API: modules & items ----------------

    def list_modules_with_items(self, course_id: int) -> List[Dict]:
        """Prefer a single call with include=items to preserve natural item order."""
        url = f"{self.base_url}/api/v1/courses/{course_id}/modules"
        return self._get_all(url, params={"per_page": 100, "include[]": "items"})

    def get_page_body(self, course_id: int, page_url: str) -> str:
        """Fetch a Canvas page body (HTML)."""
        url = f"{self.base_url}/api/v1/courses/{course_id}/pages/{page_url}"
        r = self.client.get(url)
        r.raise_for_status()
        return r.json().get("body") or ""

    @staticmethod
    def _extract_echo_embeds_from_html(html: str) -> List[Dict]:
        """Parse <iframe> embeds that look like Echo360 and return their titles (raw, cleaned)."""
        if not html:
            return []
        soup = BeautifulSoup(html, "html.parser")
        out: List[Dict] = []
        for iframe in soup.find_all("iframe"):
            src = iframe.get("src", "") or ""
            # Echo360 direct or Canvas external tools' retrieve URLs
            if ("echo360.org" not in src) and ("external_tools/retrieve" not in src):
                continue
            iframe_title = (iframe.get("title") or "").strip()
            if not iframe_title:
                continue
            cleaned = CanvasService._strip_noise(iframe_title)
            out.append(
                {
                    "video_title_raw": cleaned,                # duration etc. stripped
                    "video_title_original": iframe_title,      # original title in iframe
                }
            )
        return out

    def build_order_df(self, course_id: int) -> pd.DataFrame:
        """
        Return a DataFrame with module/item ordering and Echo video titles extracted.

        Columns (superset; some null depending on item type):
          - module (str)
          - module_position (int)
          - item_type (str)                  # 'ExternalTool','ExternalUrl','Page','Assignment','Quiz','Discussion',...
          - item_position (int)
          - item_title_raw (str)             # original Canvas item title (noise NOT stripped)
          - item_title_normalized (str)      # casefolded item title
          - video_title_raw (str|None)       # for Echo videos: cleaned title used to match Echo CSV
          - html_url (str|None)
          - external_url (str|None)
        """
        modules = self.list_modules_with_items(course_id)

        rows: List[Dict] = []
        for m in sorted(modules, key=lambda x: x.get("position", 0)):
            mod_name = m.get("name")
            mod_pos = m.get("position")
            for it in sorted(m.get("items", []), key=lambda x: x.get("position", 0)):
                item_type = it.get("type")
                title = (it.get("title") or "").strip()
                item_pos = it.get("position")
                html_url = it.get("html_url")
                external_url = it.get("external_url")

                # Default row (non-video items still useful for gradebook mapping)
                base_row = {
                    "module": mod_name,
                    "module_position": mod_pos,
                    "item_type": item_type,
                    "item_position": item_pos,
                    "item_title_raw": title,
                    "item_title_normalized": title.casefold(),
                    "video_title_raw": None,  # filled for Echo videos below
                    "html_url": html_url,
                    "external_url": external_url,
                }

                # ---- Echo videos via ExternalTool / ExternalUrl ----
                if item_type in ("ExternalTool", "ExternalUrl"):
                    url = external_url or ""
                    if "echo360.org" in url:
                        # Canvas item title typically mirrors Echo media title (with duration) → clean it
                        vr = self._strip_noise(title)
                        row = base_row.copy()
                        row["video_title_raw"] = vr
                        rows.append(row)
                        continue  # done

                # ---- Echo videos embedded inside a Page ----
                if item_type == "Page":
                    page_url = it.get("page_url")
                    try:
                        body = self.get_page_body(course_id, page_url) if page_url else ""
                    except httpx.HTTPStatusError:
                        body = ""
                    embeds = self._extract_echo_embeds_from_html(body)
                    if embeds:
                        for e in embeds:
                            row = base_row.copy()
                            row["video_title_raw"] = e["video_title_raw"]
                            rows.append(row)
                        continue  # already appended embed rows
                    # No echo embeds found → still keep the page row (non-video)
                    rows.append(base_row)
                    continue

                # ---- Other items (Assignments, Quizzes, Discussions, Files, etc.) ----
                rows.append(base_row)

        df = pd.DataFrame(rows)
        return df

    # ---------------- Enrollments (preferred student count) ----------------

    def get_student_count(self, course_id: int) -> Optional[int]:
        """
        Count unique user_id for active StudentEnrollment.
        Returns None if not permitted or empty.
        """
        url = f"{self.base_url}/api/v1/courses/{course_id}/enrollments"
        params = {"per_page": 100, "type[]": "StudentEnrollment", "state[]": "active"}
        try:
            enrollments = self._get_all(url, params=params)
        except httpx.HTTPStatusError:
            return None

        if not enrollments:
            return None

        user_ids = {e.get("user_id") for e in enrollments if e.get("user_id") is not None}
        return len(user_ids) if user_ids else None

    # ---------------- Cleanup ----------------

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
