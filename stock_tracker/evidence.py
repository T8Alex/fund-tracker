from __future__ import annotations

from fund_tracker.macro import classify_regime


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def _macro_score(row: dict, regime: dict) -> tuple[float, str]:
    labels = [str(item) for item in (regime.get("labels") or [])]
    impacts = [str(item) for item in (regime.get("impacts") or [])]
    score = 60.0
    pe = row.get("pe")
    industry = str(row.get("category") or row.get("industry") or "")

    if any("A股偏强" in label for label in labels):
        score += 10
    elif any("A股偏弱" in label for label in labels):
        score -= 10
    elif any("A股回调" in label for label in labels):
        score -= 5
    elif any("A股反弹" in label for label in labels):
        score += 3

    if any("红利占优" in label for label in labels):
        if (pe is not None and pe <= 25) or any(
            word in industry for word in ("银行", "煤炭", "电力", "公用事业", "高速公路")
        ):
            score += 5
        elif pe is not None and pe >= 40:
            score -= 4
    if any("成长占优" in label for label in labels):
        if pe is not None and pe >= 40:
            score += 2
        elif pe is not None and pe <= 20:
            score -= 1

    if any("海外高利率" in label for label in labels) and pe is not None and pe > 30:
        score -= 5
    if any("人民币偏弱" in label for label in labels):
        score -= 3
    if any("国内低利率" in label for label in labels):
        score += 2
    if any("油价偏高" in label for label in labels):
        score -= 2

    score = _clamp(score)
    reason_parts = labels[:4] + impacts[:2]
    return round(score, 1), "；".join(reason_parts) if reason_parts else "宏观数据不足，按中性处理"


def _grade(score: float) -> str:
    if score >= 80:
        return "A"
    if score >= 70:
        return "B"
    if score >= 60:
        return "C"
    return "D"


def build_evidence(rows: list[dict], macro: dict | None) -> dict:
    regime = classify_regime(macro or {})
    results: dict[str, dict] = {}
    for row in rows:
        quality = float(row.get("quality_score") or 50.0)
        trend = float(row.get("trend_score") or 50.0)
        position = float(row.get("position_score") or 50.0)
        risk = float(row.get("risk_score") or 50.0)
        macro_score, macro_reason = _macro_score(row, regime)
        total = (
            quality * 0.55
            + trend * 0.15
            + position * 0.15
            + macro_score * 0.10
            + risk * 0.05
        )
        total = round(_clamp(total), 1)
        results[str(row.get("code") or "")] = {
            "score": total,
            "grade": _grade(total),
            "quality": round(quality, 1),
            "trend": round(trend, 1),
            "position": round(position, 1),
            "macro": round(macro_score, 1),
            "risk": round(risk, 1),
            "weights": {
                "quality": 0.55,
                "trend": 0.15,
                "position": 0.15,
                "macro": 0.10,
                "risk": 0.05,
            },
            "macro_reason": macro_reason,
            "regime_labels": regime.get("labels") or [],
        }
    return {
        "as_of": (macro or {}).get("as_of"),
        "regime": regime,
        "results": results,
    }


def evidence_for(row: dict, evidence: dict | None) -> dict:
    if not evidence:
        return {}
    return (evidence.get("results") or {}).get(str(row.get("code") or ""), {})
