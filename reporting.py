"""reporting.py — 실행 결과 엑셀 저장."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def save_reports_to_excel(reports: list[dict], out_dir: str = "reports") -> str | None:
    if not reports:
        logger.warning("저장할 리포트 데이터가 없습니다.")
        return None

    import pandas as pd

    Path(out_dir).mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    path = Path(out_dir) / f"sync_report_{len(reports)}rows_{ts}.xlsx"

    df = pd.DataFrame(reports)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="sync_report")
        ws = writer.sheets["sync_report"]
        for col_cells in ws.columns:
            width = max(len(str(c.value or "")) for c in col_cells) + 2
            ws.column_dimensions[col_cells[0].column_letter].width = min(width, 60)

    print(f"✨ 리포트 저장: {path}")
    return str(path)
