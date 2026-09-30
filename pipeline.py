from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import fitz  # PyMuPDF
from PIL import Image, ImageDraw, ImageFont
from dotenv import load_dotenv

load_dotenv()


ANALYZE_PROMPT = r'''
あなたは大学医学講義スライドを「学習可能な最小知識単位」に分解する解析器です。
入力は1ページ分の (1) PDF抽出テキスト と (2) ページ画像です。

厳守事項:
- 原資料に明示された内容だけを使い、外部知識で補完・訂正しない。
- 1 fact = 1つの関係・1つの答え。独立事項を1 factにまとめない。
- 「合成場所と輸送経路」「所属と機能」のような複合事項は必ず別factにする。
- 「LH・FSH」のように資料上1組として扱う並列項目は1objectとしてよい。
- 医学用語・数値・単位は資料表記を優先し、曖昧なら推測しない。

本文:
- 本文・箇条書きから試験で問える関係を subject / relation / object の単位で抽出する。

図・模式図・表:
- 図を要約するだけでなく、矢印・抑制線・分岐・標的臓器・作用を系統的に走査する。
- A→B→C は A→B と B→C に分ける。
- 例: TRH→TSH→甲状腺→T3,T4 は TRH→TSH、TSH→甲状腺、甲状腺→T3,T4 を別関係として抽出する。
- GHRH→GH、CRH→ACTH、ドパミン→PRL抑制なども取りこぼさない。
- 図由来の関係は visual_relations に列挙する。本文との重複は後段で除去するので許容する。

visual_dependency:
- none: 文章だけで完全に学習可能。図から得た関係でも文章化すれば解けるなら none。
- helpful: 図は理解の助けだが、解答に画像は不要。
- required: 画像の読影、位置、形、視覚所見を見ないと設問が成立しない。
- 「図に書いてある」だけでは required にしない。

画像領域:
- image_regions は独立したMRI/CT/X線/病理/模式図/表/グラフ等を列挙する。
- bbox はページ全体を1000×1000とする [x1,y1,x2,y2]。
- region_id は r1,r2,...。

fact固有 evidence_bbox:
- visual_dependency=required のfactでは、問題に必要な視覚所見を含む最小限の範囲をページ座標で指定する。
- none/helpful では null でよい。

重要度は1〜5。
JSONのみ返す。
{
  "page_summary":{"sections":["..."],"content_review":"本文概要","visual_review":"図表概要"},
  "image_regions":[{"region_id":"r1","region_type":"mri|ct|xray|ecg|pathology|diagram|table|graph|photo|other","bbox":[100,120,500,450],"description":"領域説明","usable_for_question":true}],
  "facts":[{"category":"生理","subject":"対象","relation":"関係","object":"答え","fact":"独立した事実","source_type":"text|figure|table|mixed","importance":5,"reason":"理由","image_region_id":null,"visual_dependency":"none|helpful|required","evidence_bbox":null}],
  "visual_relations":[{"category":"生理","subject":"TRH","relation":"分泌を促進する","object":"TSH","fact":"TRHはTSH分泌を促進する。","importance":5,"reason":"図中の主要関係","image_region_id":"r1","visual_dependency":"none","evidence_bbox":null}]
}
'''

QA_PROMPT = r'''
あなたは医学講義資料からAnki用一問一答を作る編集者です。factsとimage_regionsだけを使い、外部知識は追加しません。

1カード=1知識を厳守:
- 「どこで合成され、どの経路を通るか」のような2項目質問は禁止し、別カードにする。
- 「何の一部であり、どの機能を持つか」のような複合質問は禁止する。
- Backに「合成場所:... / 経路:...」のような独立項目を複数並べない。
- ただし「LH・FSHの2つは？」のような資料上1セットの問いはよい。

画像判定:
- 図由来でも文章だけで問えるなら text_only / requires_image=false。
- requires_image=true は画像がなければ意図した認識問題が成立しない場合だけ。
- visual_dependency=none/helpful の通常カードは text_only。
- visual_dependency=required のMRI/CT/病理/位置同定などは image_based。
- 画像を付けるためだけに hybrid にしない。
- source fact に evidence_bbox があれば後段でそのcropを優先する。

Image Occlusion:
- 模式図/表で答えラベルを隠すこと自体に学習価値がある場合のみ作る。
- occlusion_targetは図中に実在する短い文字列。

各カードに image_decision_reason を付ける。
JSONのみ返す。
{
  "cards":[{"source_fact_id":"p1_f001","front":"質問","back":"回答","tags":["内分泌"],"question_type":"text_only|image_based|hybrid|image_occlusion","requires_image":false,"image_region_id":null,"image_decision_reason":"理由","occlusion_candidate":false,"occlusion_target":null}]
}
'''

OCCLUSION_PROMPT = r'''
あなたは学習用Image Occlusionの位置検出器です。
入力画像は講義スライドから切り抜いた図・表・模式図です。
後ろに示す targets の各 target_text が画像内のどこに表示されているかを特定してください。

厳守事項:
- target_text と同じ意味の別の文字は選ばず、できるだけ実際に表示された対象文字列そのものを囲む。
- bbox は入力画像全体を横1000×縦1000とした正規化座標 [x1,y1,x2,y2]。
- 文字列だけを隠せる程度にタイトに囲む。ただし文字が切れないよう少し余白を含める。
- 同じ文字列が複数ある場合、カードのsource_factに最も対応するものを選ぶ。
- 確信できない場合は found=false にする。推測しない。
- 複数行の答えで、離れた位置を隠す必要がある場合はbboxesを複数返してよい。
- JSONのみ返す。

出力形式:
{
  "targets": [
    {
      "temp_card_id": "c1",
      "target_text": "TSH",
      "found": true,
      "matched_text": "TSH",
      "bboxes": [[450,380,520,440]]
    }
  ]
}
'''

def default_model(provider: str) -> str:
    provider = (provider or "gemini").strip().lower()
    if provider == "openai":
        return os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
    if provider == "gemini":
        return os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
    raise ValueError(f"Unsupported AI provider: {provider}")


@dataclass
class Config:
    provider: str = os.getenv("AI_PROVIDER", "gemini")
    model: str = ""
    min_importance: int = 3
    dpi: int = 160
    max_retries: int = 3
    sleep_seconds: float = 1.0
    enable_image_cards: bool = True
    enable_image_occlusion: bool = True
    crop_padding: float = 0.03
    occlusion_padding: float = 0.08

    # v5.2 resilience
    fallback_models: str = os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash-lite")
    min_request_interval_seconds: float = float(
        os.getenv("GEMINI_MIN_REQUEST_INTERVAL_SECONDS", "15")
    )
    max_retry_wait_seconds: float = float(
        os.getenv("AI_MAX_RETRY_WAIT_SECONDS", "120")
    )

    def __post_init__(self) -> None:
        self.provider = (self.provider or "gemini").strip().lower()
        if self.provider not in {"openai", "gemini"}:
            raise ValueError("provider は openai または gemini を指定してください。")
        if not self.model:
            self.model = default_model(self.provider)

        self.max_retries = max(1, int(self.max_retries))
        self.min_request_interval_seconds = max(0.0, float(self.min_request_interval_seconds))
        self.max_retry_wait_seconds = max(1.0, float(self.max_retry_wait_seconds))

        # Fallback is mainly for Gemini. OpenAI defaults to no automatic model switch.
        if self.provider != "gemini":
            self.fallback_models = ""

    def fallback_model_list(self) -> list[str]:
        result: list[str] = []
        for value in str(self.fallback_models or "").split(","):
            value = value.strip()
            if value and value != self.model and value not in result:
                result.append(value)
        return result


def safe_name(name: str) -> str:
    name = re.sub(r"[^0-9A-Za-zぁ-んァ-ヶ一-龠._-]+", "_", name)
    return name.strip("_") or "document"


def image_to_data_url(path: Path) -> str:
    mime = "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def parse_json_text(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def create_ai_client(provider: str):
    provider = (provider or "").strip().lower()

    if provider == "openai":
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError(
                "OPENAI_API_KEY が設定されていません。.env または画面入力で設定してください。"
            )
        from openai import OpenAI
        return OpenAI()

    if provider == "gemini":
        if not os.getenv("GEMINI_API_KEY"):
            raise RuntimeError(
                "GEMINI_API_KEY が設定されていません。.env または画面入力で設定してください。"
            )
        from google import genai
        return genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

    raise ValueError(f"Unsupported AI provider: {provider}")


def _gemini_json_config():
    from google.genai import types
    return types.GenerateContentConfig(
        response_mime_type="application/json",
        temperature=0.2,
    )


_LAST_REQUEST_AT: dict[str, float] = {}


def _notify(status_callback: Callable[[str], None] | None, message: str) -> None:
    print(message)
    if status_callback:
        try:
            status_callback(message)
        except Exception:
            pass


def _extract_retry_after_seconds(exc: Exception) -> float | None:
    text = str(exc)

    patterns = [
        r"Please retry in\s+([0-9]+(?:\.[0-9]+)?)s",
        r"retryDelay['\"]?\s*:\s*['\"]([0-9]+(?:\.[0-9]+)?)s",
        r"retry in\s+([0-9]+(?:\.[0-9]+)?)s",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            try:
                return float(m.group(1))
            except Exception:
                pass
    return None


def _transient_error_kind(exc: Exception) -> str | None:
    text = str(exc).upper()
    status_code = getattr(exc, "status_code", None)
    code = getattr(exc, "code", None)

    if status_code == 429 or code == 429 or "429" in text or "RESOURCE_EXHAUSTED" in text:
        return "429"
    if status_code == 503 or code == 503 or "503" in text or "UNAVAILABLE" in text:
        return "503"
    return None


def _pace_request(provider: str, model: str, min_interval_seconds: float,
                  status_callback: Callable[[str], None] | None) -> None:
    if provider != "gemini" or min_interval_seconds <= 0:
        return

    key = f"{provider}:{model}"
    now = time.monotonic()
    last = _LAST_REQUEST_AT.get(key)
    if last is not None:
        wait = min_interval_seconds - (now - last)
        if wait > 0:
            _notify(
                status_callback,
                f"Gemini無料枠対策: 次のAPI呼び出しまで {wait:.1f} 秒待機します。"
            )
            time.sleep(wait)

    _LAST_REQUEST_AT[key] = time.monotonic()


def _retry_wait_seconds(kind: str, exc: Exception, attempt: int,
                        max_retry_wait_seconds: float) -> float:
    hinted = _extract_retry_after_seconds(exc)
    if kind == "429":
        # Respect Gemini's RetryInfo when present. A small margin avoids retrying
        # exactly on the boundary of a per-minute quota window.
        wait = (hinted + 2.0) if hinted is not None else max(15.0, 30.0 * (attempt + 1))
    else:
        # 503 high-demand errors: progressively wait longer.
        wait = 15.0 * (2 ** attempt)
        if hinted is not None:
            wait = max(wait, hinted + 2.0)

    return min(float(wait), float(max_retry_wait_seconds))


def _model_candidates(provider: str, primary_model: str,
                      fallback_models: list[str] | None) -> list[str]:
    models = [primary_model]
    if provider == "gemini":
        for model in fallback_models or []:
            model = str(model).strip()
            if model and model not in models:
                models.append(model)
    return models


def _run_resilient(
    provider: str,
    primary_model: str,
    fallback_models: list[str] | None,
    max_retries: int,
    min_request_interval_seconds: float,
    max_retry_wait_seconds: float,
    operation: Callable[[str], dict[str, Any]],
    status_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    last_err: Exception | None = None
    models = _model_candidates(provider, primary_model, fallback_models)

    for model_index, active_model in enumerate(models):
        if model_index > 0:
            _notify(
                status_callback,
                f"主モデルで処理できなかったため、フォールバックモデル {active_model} を試します。"
            )

        for attempt in range(max_retries):
            _pace_request(
                provider,
                active_model,
                min_request_interval_seconds,
                status_callback,
            )

            try:
                result = operation(active_model)
                if active_model != primary_model:
                    result = dict(result)
                    result.setdefault("_used_model", active_model)
                return result

            except Exception as exc:
                last_err = exc
                kind = _transient_error_kind(exc)

                # Authentication, invalid model, malformed request, etc. should fail fast.
                if kind is None:
                    raise

                if attempt + 1 >= max_retries:
                    _notify(
                        status_callback,
                        f"{active_model}: {kind} が {max_retries} 回続きました。"
                    )
                    break

                wait = _retry_wait_seconds(
                    kind, exc, attempt, max_retry_wait_seconds
                )
                if kind == "429":
                    reason = "利用上限"
                else:
                    reason = "一時的な高負荷"
                _notify(
                    status_callback,
                    f"{active_model}: {reason} ({kind})。{wait:.0f} 秒待って再試行します "
                    f"({attempt + 2}/{max_retries})。"
                )
                time.sleep(wait)

    assert last_err is not None
    raise last_err


def call_vision_json(provider: str, client, model: str, prompt: str, text: str,
                     image_path: Path, max_retries: int = 3,
                     fallback_models: list[str] | None = None,
                     min_request_interval_seconds: float = 0.0,
                     max_retry_wait_seconds: float = 120.0,
                     status_callback: Callable[[str], None] | None = None) -> dict[str, Any]:

    merged_prompt = prompt + "\n\nPDF抽出テキスト:\n" + text[:30000]

    def operation(active_model: str) -> dict[str, Any]:
        if provider == "openai":
            response = client.responses.create(
                model=active_model,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": merged_prompt},
                            {
                                "type": "input_image",
                                "image_url": image_to_data_url(image_path),
                                "detail": "high",
                            },
                        ],
                    }
                ],
            )
            return parse_json_text(response.output_text)

        if provider == "gemini":
            with Image.open(image_path) as img:
                image = img.convert("RGB").copy()
            response = client.models.generate_content(
                model=active_model,
                contents=[merged_prompt, image],
                config=_gemini_json_config(),
            )
            return parse_json_text(response.text or "")

        raise ValueError(f"Unsupported AI provider: {provider}")

    return _run_resilient(
        provider=provider,
        primary_model=model,
        fallback_models=fallback_models,
        max_retries=max_retries,
        min_request_interval_seconds=min_request_interval_seconds,
        max_retry_wait_seconds=max_retry_wait_seconds,
        operation=operation,
        status_callback=status_callback,
    )


def call_text_json(provider: str, client, model: str, prompt: str,
                   payload: dict[str, Any], max_retries: int = 3,
                   fallback_models: list[str] | None = None,
                   min_request_interval_seconds: float = 0.0,
                   max_retry_wait_seconds: float = 120.0,
                   status_callback: Callable[[str], None] | None = None) -> dict[str, Any]:

    merged_prompt = prompt + "\n\n入力JSON:\n" + json.dumps(payload, ensure_ascii=False)

    def operation(active_model: str) -> dict[str, Any]:
        if provider == "openai":
            response = client.responses.create(
                model=active_model,
                input=merged_prompt,
            )
            return parse_json_text(response.output_text)

        if provider == "gemini":
            response = client.models.generate_content(
                model=active_model,
                contents=merged_prompt,
                config=_gemini_json_config(),
            )
            return parse_json_text(response.text or "")

        raise ValueError(f"Unsupported AI provider: {provider}")

    return _run_resilient(
        provider=provider,
        primary_model=model,
        fallback_models=fallback_models,
        max_retries=max_retries,
        min_request_interval_seconds=min_request_interval_seconds,
        max_retry_wait_seconds=max_retry_wait_seconds,
        operation=operation,
        status_callback=status_callback,
    )


def call_image_json(provider: str, client, model: str, prompt: str,
                    image_path: Path, max_retries: int = 3,
                    fallback_models: list[str] | None = None,
                    min_request_interval_seconds: float = 0.0,
                    max_retry_wait_seconds: float = 120.0,
                    status_callback: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Call a vision-capable provider with one image and require JSON text output."""

    def operation(active_model: str) -> dict[str, Any]:
        if provider == "openai":
            response = client.responses.create(
                model=active_model,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": prompt},
                            {
                                "type": "input_image",
                                "image_url": image_to_data_url(image_path),
                                "detail": "high",
                            },
                        ],
                    }
                ],
            )
            return parse_json_text(response.output_text)

        if provider == "gemini":
            with Image.open(image_path) as img:
                image = img.convert("RGB").copy()
            response = client.models.generate_content(
                model=active_model,
                contents=[prompt, image],
                config=_gemini_json_config(),
            )
            return parse_json_text(response.text or "")

        raise ValueError(f"Unsupported AI provider: {provider}")

    return _run_resilient(
        provider=provider,
        primary_model=model,
        fallback_models=fallback_models,
        max_retries=max_retries,
        min_request_interval_seconds=min_request_interval_seconds,
        max_retry_wait_seconds=max_retry_wait_seconds,
        operation=operation,
        status_callback=status_callback,
    )


def render_and_extract(pdf_path: Path, work_dir: Path, dpi: int) -> list[dict[str, Any]]:
    images_dir = work_dir / "pages"
    images_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(pdf_path)
    pages: list[dict[str, Any]] = []
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    for i, page in enumerate(doc):
        page_no = i + 1
        image_path = images_dir / f"page_{page_no:04d}.jpg"
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        pix.save(image_path)
        text = page.get_text("text") or ""
        pages.append({"page": page_no, "text": text, "image_path": image_path})
    doc.close()
    return pages


def clamp(v: float, low: float = 0.0, high: float = 1000.0) -> float:
    return max(low, min(high, float(v)))


def valid_bbox(bbox: Any) -> list[float] | None:
    if not isinstance(bbox, list) or len(bbox) != 4:
        return None
    try:
        x1, y1, x2, y2 = [clamp(float(v)) for v in bbox]
    except (TypeError, ValueError):
        return None
    if x2 <= x1 or y2 <= y1:
        return None
    if (x2 - x1) < 15 or (y2 - y1) < 15:
        return None
    return [round(x1, 2), round(y1, 2), round(x2, 2), round(y2, 2)]


def _norm_text_for_dedupe(text: Any) -> str:
    t = str(text or "").strip().lower()
    return re.sub(r"[\s。．、,，:：;；・()（）]+", "", t)


def normalize_analysis(page_no: int, analyzed: dict[str, Any]) -> dict[str, Any]:
    """Assign IDs, normalize visual metadata, and merge systematic visual relations."""
    regions_out: list[dict[str, Any]] = []
    local_to_global: dict[str, str] = {}
    for i, region in enumerate(analyzed.get("image_regions", []) or [], start=1):
        bbox = valid_bbox(region.get("bbox"))
        if bbox is None:
            continue
        local_id = str(region.get("region_id") or f"r{i}")
        global_id = f"p{page_no}_img{i:03d}"
        local_to_global[local_id] = global_id
        regions_out.append({
            "image_region_id": global_id,
            "model_region_id": local_id,
            "page": page_no,
            "region_type": str(region.get("region_type") or "other"),
            "bbox_norm": bbox,
            "description": str(region.get("description") or ""),
            "usable_for_question": bool(region.get("usable_for_question", True)),
        })

    raw_facts = list(analyzed.get("facts", []) or [])
    for relation in analyzed.get("visual_relations", []) or []:
        item = dict(relation)
        item.setdefault("source_type", "figure")
        item.setdefault("visual_dependency", "none")
        if not item.get("fact"):
            subj = str(item.get("subject") or "").strip()
            rel = str(item.get("relation") or "").strip()
            obj = str(item.get("object") or "").strip()
            item["fact"] = f"{subj}{rel}{obj}".strip()
        raw_facts.append(item)

    facts_out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for fact in raw_facts:
        fact_text = str(fact.get("fact") or "").strip()
        subject = str(fact.get("subject") or "").strip()
        relation = str(fact.get("relation") or "").strip()
        obj = str(fact.get("object") or "").strip()
        if not fact_text and (subject or relation or obj):
            fact_text = f"{subject}{relation}{obj}".strip()
        if not fact_text:
            continue
        key = _norm_text_for_dedupe(fact_text)
        if key in seen:
            continue
        seen.add(key)
        i = len(facts_out) + 1
        item = dict(fact)
        item["fact"] = fact_text
        item["subject"] = subject
        item["relation"] = relation
        item["object"] = obj
        item["fact_id"] = f"p{page_no}_f{i:03d}"
        item["page"] = page_no
        local_region_id = fact.get("image_region_id")
        item["image_region_id"] = local_to_global.get(str(local_region_id)) if local_region_id else None
        dep = str(fact.get("visual_dependency") or "none").lower()
        item["visual_dependency"] = dep if dep in {"none", "helpful", "required"} else "none"
        item["evidence_bbox_norm"] = valid_bbox(fact.get("evidence_bbox"))
        facts_out.append(item)

    return {"page_summary": analyzed.get("page_summary", {}), "image_regions": regions_out, "facts": facts_out}


def bbox_norm_to_px(bbox_norm: list[float], width: int, height: int, padding: float = 0.03) -> list[int]:
    x1, y1, x2, y2 = bbox_norm
    px = [x1 / 1000 * width, y1 / 1000 * height, x2 / 1000 * width, y2 / 1000 * height]
    pad_x = (px[2] - px[0]) * padding
    pad_y = (px[3] - px[1]) * padding
    x1p = max(0, int(round(px[0] - pad_x)))
    y1p = max(0, int(round(px[1] - pad_y)))
    x2p = min(width, int(round(px[2] + pad_x)))
    y2p = min(height, int(round(px[3] + pad_y)))
    return [x1p, y1p, x2p, y2p]


def crop_image_regions(page_image: Path, image_regions: list[dict[str, Any]], media_dir: Path,
                       pdf_prefix: str, padding: float = 0.03) -> list[dict[str, Any]]:
    media_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(page_image) as img:
        width, height = img.size
        for region in image_regions:
            bbox_norm = region.get("bbox_norm")
            if not bbox_norm:
                continue
            bbox_px = bbox_norm_to_px(bbox_norm, width, height, padding)
            x1, y1, x2, y2 = bbox_px
            if x2 <= x1 or y2 <= y1:
                continue
            crop = img.crop((x1, y1, x2, y2)).convert("RGB")
            image_region_id = region["image_region_id"]
            filename = safe_name(f"{pdf_prefix}_{image_region_id}.jpg")
            out = media_dir / filename
            crop.save(out, "JPEG", quality=92, optimize=True)
            region["bbox_px"] = bbox_px
            region["image_file"] = filename
            region["image_path"] = f"media/{filename}"
    return image_regions


def crop_required_fact_evidence(page_image: Path, facts: list[dict[str, Any]], media_dir: Path,
                                pdf_prefix: str, padding: float = 0.05) -> list[dict[str, Any]]:
    """Crop fact-specific visual evidence only for required image questions."""
    out = [dict(f) for f in facts]
    with Image.open(page_image) as img:
        width, height = img.size
        for fact in out:
            fact["evidence_bbox_px"] = None
            fact["evidence_image_file"] = None
            fact["evidence_image_path"] = None
            if str(fact.get("visual_dependency") or "none") != "required":
                continue
            bbox = valid_bbox(fact.get("evidence_bbox_norm"))
            if bbox is None:
                continue
            px = bbox_norm_to_px(bbox, width, height, padding)
            x1, y1, x2, y2 = px
            if x2 <= x1 or y2 <= y1:
                continue
            crop = img.crop((x1, y1, x2, y2)).convert("RGB")
            filename = safe_name(f"{pdf_prefix}_{fact['fact_id']}_evidence.jpg")
            path = media_dir / filename
            crop.save(path, "JPEG", quality=92, optimize=True)
            fact["evidence_bbox_px"] = px
            fact["evidence_image_file"] = filename
            fact["evidence_image_path"] = f"media/{filename}"
    return out


def enrich_facts_with_regions(facts: list[dict[str, Any]], regions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {r["image_region_id"]: r for r in regions}
    out = []
    for fact in facts:
        item = dict(fact)
        region = by_id.get(item.get("image_region_id"))
        if region:
            item["region_bbox_norm"] = region.get("bbox_norm")
            item["region_bbox_px"] = region.get("bbox_px")
            item["region_image_path"] = region.get("image_path")
            item["region_image_file"] = region.get("image_file")
            item["region_type"] = region.get("region_type")
            item["region_description"] = region.get("description")
        else:
            item["region_bbox_norm"] = None
            item["region_bbox_px"] = None
            item["region_image_path"] = None
            item["region_image_file"] = None
            item["region_type"] = None
            item["region_description"] = None
        out.append(item)
    return out


def _compound_card_reason(front: str, back: str) -> str | None:
    f = str(front or "")
    b = str(back or "")
    patterns = [
        (r"どこで.*どの(?:経路|ような経路|方法)", "複数事項: 場所と経路"),
        (r"合成(?:場所|部位).*経路", "複数事項: 合成場所と経路"),
        (r"何の一部.*どのような(?:機能|役割)", "複数事項: 所属と機能"),
        (r"どこで.*何を通", "複数事項: 場所と輸送経路"),
    ]
    for pattern, reason in patterns:
        if re.search(pattern, f):
            return reason
    if re.search(r"(?:合成場所|部位|経路|作用|機能)[：:]", b) and "\n" in b:
        labels = re.findall(r"(?:合成場所|部位|経路|作用|機能)[：:]", b)
        if len(labels) >= 2:
            return "Backに独立した複数項目"
    return None


def attach_card_region_metadata(cards: list[dict[str, Any]], regions: list[dict[str, Any]],
                                enable_image_cards: bool,
                                facts: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Enforce image necessity and bind image cards to fact-specific evidence when possible."""
    by_id = {r["image_region_id"]: r for r in regions}
    by_fact = {str(f.get("fact_id")): f for f in (facts or [])}
    out = []
    for card in cards:
        item = dict(card)
        fact = by_fact.get(str(item.get("source_fact_id") or ""), {})
        dep = str(fact.get("visual_dependency") or "none")
        qtype = str(item.get("question_type") or "text_only")
        is_occlusion = qtype == "image_occlusion"
        item["visual_dependency"] = dep
        item.setdefault("image_decision_reason", "")
        item["image_validation_status"] = "not_applicable"
        item["quality_status"] = "ok"
        item["quality_flags"] = []

        compound_reason = _compound_card_reason(item.get("front", ""), item.get("back", ""))
        if compound_reason:
            item["quality_status"] = "needs_review"
            item["quality_flags"].append(compound_reason)
            item["anki_ready"] = False

        # Strict: ordinary cards need images only when the source fact is visually required.
        if not is_occlusion and dep != "required":
            if qtype in {"image_based", "hybrid"} or bool(item.get("requires_image", False)):
                item["quality_flags"].append("画像不要の知識をtext_onlyへ自動修正")
            item["question_type"] = "text_only"
            item["requires_image"] = False
            item["image_region_id"] = None
            item["crop_bbox_norm"] = None
            item["crop_bbox_px"] = None
            item["image_path"] = None
            item["image_file"] = None
            item["image_validation_status"] = "forced_text_only"
            if not item.get("image_decision_reason"):
                item["image_decision_reason"] = "画像なしで同じ知識を問えるため"
            out.append(item)
            continue

        requires_image = (bool(item.get("requires_image", False)) or is_occlusion or dep == "required") and enable_image_cards
        if dep == "required" and not is_occlusion and qtype == "text_only":
            item["question_type"] = "image_based"
        item["requires_image"] = requires_image

        if not requires_image:
            item["image_region_id"] = None
            item["crop_bbox_norm"] = None
            item["crop_bbox_px"] = None
            item["image_path"] = None
            item["image_file"] = None
            out.append(item)
            continue

        if is_occlusion:
            region_id = item.get("image_region_id") or fact.get("image_region_id")
            region = by_id.get(region_id) if region_id else None
            if not region:
                item["requires_image"] = False
                item["question_type"] = "text_only"
                item["occlusion_candidate"] = False
                item["quality_status"] = "needs_review"
                item["quality_flags"].append("Image Occlusion用の図領域が見つからない")
                item["anki_ready"] = False
                item["image_validation_status"] = "missing_region"
            else:
                item["image_region_id"] = region_id
                item["crop_bbox_norm"] = region.get("bbox_norm")
                item["crop_bbox_px"] = region.get("bbox_px")
                item["image_path"] = region.get("image_path")
                item["image_file"] = region.get("image_file")
                item["image_validation_status"] = "diagram_region"
            out.append(item)
            continue

        if fact.get("evidence_image_path"):
            item["image_region_id"] = fact.get("image_region_id")
            item["crop_bbox_norm"] = fact.get("evidence_bbox_norm")
            item["crop_bbox_px"] = fact.get("evidence_bbox_px")
            item["image_path"] = fact.get("evidence_image_path")
            item["image_file"] = fact.get("evidence_image_file")
            item["image_validation_status"] = "fact_evidence_crop"
        else:
            region_id = item.get("image_region_id") or fact.get("image_region_id")
            region = by_id.get(region_id) if region_id else None
            if region:
                item["image_region_id"] = region_id
                item["crop_bbox_norm"] = region.get("bbox_norm")
                item["crop_bbox_px"] = region.get("bbox_px")
                item["image_path"] = region.get("image_path")
                item["image_file"] = region.get("image_file")
                item["image_validation_status"] = "region_fallback"
                item["quality_flags"].append("fact固有cropなし: 図領域を代用")
            else:
                item["requires_image"] = False
                item["question_type"] = "text_only"
                item["quality_status"] = "needs_review"
                item["quality_flags"].append("画像必須factだが有効なcropがない")
                item["anki_ready"] = False
                item["image_region_id"] = None
                item["crop_bbox_norm"] = None
                item["crop_bbox_px"] = None
                item["image_path"] = None
                item["image_file"] = None
                item["image_validation_status"] = "missing_visual_evidence"
        out.append(item)
    return out


def draw_occlusion_mask(source_image: Path, output_image: Path,
                        bboxes_norm: list[list[float]], padding: float = 0.08) -> list[list[int]]:
    """Create one occluded image and return the pixel-space masks actually applied."""
    with Image.open(source_image) as img:
        canvas = img.convert("RGB")
        draw = ImageDraw.Draw(canvas)
        width, height = canvas.size
        bboxes_px: list[list[int]] = []
        for bbox in bboxes_norm:
            valid = valid_bbox(bbox)
            if valid is None:
                continue
            px = bbox_norm_to_px(valid, width, height, padding)
            x1, y1, x2, y2 = px
            if x2 <= x1 or y2 <= y1:
                continue
            # Neutral mask with a border and question mark; keeps the surrounding visual context intact.
            draw.rectangle((x1, y1, x2, y2), fill=(245, 245, 245), outline=(70, 70, 70), width=2)
            try:
                font = ImageFont.load_default()
                label = "?"
                box = draw.textbbox((0, 0), label, font=font)
                tw, th = box[2] - box[0], box[3] - box[1]
                draw.text(((x1 + x2 - tw) / 2, (y1 + y2 - th) / 2), label, fill=(30, 30, 30), font=font)
            except Exception:
                pass
            bboxes_px.append(px)
        output_image.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(output_image, "JPEG", quality=92, optimize=True)
        return bboxes_px


def apply_image_occlusions(provider: str, client, model: str, cards: list[dict[str, Any]],
                           regions: list[dict[str, Any]], media_dir: Path,
                           pdf_prefix: str, page_no: int, max_retries: int,
                           enabled: bool, padding: float = 0.08,
                           fallback_models: list[str] | None = None,
                           min_request_interval_seconds: float = 0.0,
                           max_retry_wait_seconds: float = 120.0,
                           status_callback: Callable[[str], None] | None = None) -> list[dict[str, Any]]:
    """Locate answer labels inside diagram crops and generate per-card occluded images."""
    out = [dict(c) for c in cards]
    for i, card in enumerate(out, start=1):
        card.setdefault("temp_card_id", f"p{page_no}_c{i:03d}")
        card.setdefault("occlusion_candidate", False)
        card.setdefault("occlusion_target", None)
        card.setdefault("occlusion_applied", False)
        card.setdefault("occlusion_status", "not_requested")
        card.setdefault("occlusion_bbox_norm", None)
        card.setdefault("occlusion_bbox_px", None)
        card.setdefault("occluded_image_file", None)
        card.setdefault("occluded_image_path", None)
        card.setdefault("anki_ready", True)

    if not enabled:
        for card in out:
            if card.get("question_type") == "image_occlusion":
                card["occlusion_status"] = "disabled"
                card["anki_ready"] = False
        return out

    by_region = {r.get("image_region_id"): r for r in regions}
    grouped: dict[str, list[dict[str, Any]]] = {}

    for card in out:
        qtype = str(card.get("question_type") or "text_only")
        candidate = bool(card.get("occlusion_candidate", False)) or qtype == "image_occlusion"
        target = str(card.get("occlusion_target") or "").strip()
        region_id = card.get("image_region_id")
        region = by_region.get(region_id)
        if not candidate:
            continue
        if not target or not region or not region.get("image_file"):
            card["occlusion_status"] = "missing_target_or_region"
            card["anki_ready"] = False if qtype == "image_occlusion" else True
            continue
        # Occlusion is mainly for labelled diagrams/tables/graphs. Do not mask anatomical structures on MRI/CT.
        if str(region.get("region_type") or "").lower() not in {"diagram", "table", "graph", "other"}:
            card["occlusion_status"] = "unsupported_region_type"
            card["anki_ready"] = False if qtype == "image_occlusion" else True
            continue
        grouped.setdefault(str(region_id), []).append(card)

    for region_id, region_cards in grouped.items():
        region = by_region[region_id]
        source_image = media_dir / str(region.get("image_file"))
        if not source_image.exists():
            for card in region_cards:
                card["occlusion_status"] = "source_image_missing"
                card["anki_ready"] = False
            continue

        targets = [
            {
                "temp_card_id": c["temp_card_id"],
                "target_text": c.get("occlusion_target"),
                "source_fact_id": c.get("source_fact_id"),
                "answer": c.get("back"),
            }
            for c in region_cards
        ]
        prompt = OCCLUSION_PROMPT + "\n\ntargets:\n" + json.dumps(targets, ensure_ascii=False)
        located = call_image_json(
            provider, client, model, prompt, source_image, max_retries,
            fallback_models=fallback_models,
            min_request_interval_seconds=min_request_interval_seconds,
            max_retry_wait_seconds=max_retry_wait_seconds,
            status_callback=status_callback,
        )
        results = {
            str(x.get("temp_card_id")): x
            for x in (located.get("targets", []) or [])
            if x.get("temp_card_id")
        }

        for card in region_cards:
            result = results.get(card["temp_card_id"], {})
            found = bool(result.get("found", False))
            masks = []
            for bbox in (result.get("bboxes", []) or []):
                vb = valid_bbox(bbox)
                if vb is not None:
                    masks.append(vb)

            if not found or not masks:
                card["occlusion_status"] = "not_found"
                card["anki_ready"] = False
                continue

            out_name = safe_name(
                f"{pdf_prefix}_{region_id}_{card['temp_card_id']}_occlusion.jpg"
            )
            out_path = media_dir / out_name
            masks_px = draw_occlusion_mask(source_image, out_path, masks, padding)
            if not masks_px:
                card["occlusion_status"] = "mask_invalid"
                card["anki_ready"] = False
                continue

            card["occlusion_applied"] = True
            card["occlusion_status"] = "applied"
            card["occlusion_bbox_norm"] = masks
            card["occlusion_bbox_px"] = masks_px
            card["occluded_image_file"] = out_name
            card["occluded_image_path"] = f"media/{out_name}"
            card["anki_ready"] = True

    return out


def deduplicate_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Conservative exact-normalized dedupe; keeps image and text cards distinct."""
    seen: set[str] = set()
    out = []
    for card in cards:
        key_text = (
            card.get("front", "") + "\n" + card.get("back", "") + "\n" +
            str(card.get("requires_image", False)) + "\n" + str(card.get("image_region_id", ""))
        ).strip().lower()
        key_text = re.sub(r"\s+", "", key_text)
        key = hashlib.sha256(key_text.encode("utf-8")).hexdigest()
        if key not in seen:
            seen.add(key)
            out.append(card)
    return out


def write_csv(path: Path, headers: list[str], rows: list[list[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)


def csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def build_outputs(pdf_path: Path, output_dir: Path, page_results: list[dict[str, Any]],
                  min_importance: int) -> dict[str, int]:
    page_review_rows = []
    region_rows = []
    fact_rows = []
    importance_rows = []
    qa_rows = []
    anki_rows = []
    review_rows = []

    all_cards: list[dict[str, Any]] = []
    total_facts = 0
    selected_facts = 0

    for pr in page_results:
        p = pr["page"]
        summary = pr.get("page_summary", {})
        page_review_rows.append([
            p,
            " / ".join(summary.get("sections", [])),
            summary.get("content_review", ""),
            summary.get("visual_review", ""),
        ])

        for region in pr.get("image_regions", []):
            region_rows.append([
                region.get("image_region_id", ""), p, region.get("region_type", ""),
                region.get("description", ""), csv_value(region.get("bbox_norm")),
                csv_value(region.get("bbox_px")), region.get("image_path", ""),
                region.get("usable_for_question", False),
            ])

        for fact in pr.get("facts", []):
            total_facts += 1
            imp = int(fact.get("importance", 0) or 0)
            selected = imp >= min_importance
            if selected:
                selected_facts += 1
            fact_rows.append([
                fact.get("fact_id", ""), fact.get("category", ""), fact.get("fact", ""),
                fact.get("source_type", ""), p, fact.get("image_region_id", ""),
                fact.get("visual_dependency", "none"),
                fact.get("subject", ""), fact.get("relation", ""), fact.get("object", ""),
                csv_value(fact.get("evidence_bbox_norm")), csv_value(fact.get("evidence_bbox_px")),
                fact.get("evidence_image_path", ""), fact.get("region_image_path", ""),
            ])
            importance_rows.append([
                fact.get("fact_id", ""), imp, "採用" if selected else "除外",
                fact.get("reason", ""), p,
                fact.get("visual_dependency", "none") == "required",
                fact.get("image_region_id", ""), csv_value(fact.get("evidence_bbox_norm")),
                fact.get("evidence_image_path", "") or fact.get("region_image_path", ""),
            ])

        for card in pr.get("cards", []):
            item = dict(card)
            item["page"] = p
            all_cards.append(item)

    all_cards = deduplicate_cards(all_cards)
    image_card_count = 0
    occlusion_card_count = 0
    occlusion_failed_count = 0
    anki_ready_count = 0
    quality_review_count = 0

    for idx, card in enumerate(all_cards, start=1):
        tags = " ".join(card.get("tags", []))
        source = f"{pdf_path.name} p.{card.get('page', '')}"
        requires_image = bool(card.get("requires_image", False))
        if requires_image:
            image_card_count += 1

        occlusion_applied = bool(card.get("occlusion_applied", False))
        occlusion_status = str(card.get("occlusion_status") or "not_requested")
        if occlusion_applied:
            occlusion_card_count += 1
        elif str(card.get("question_type") or "") == "image_occlusion":
            occlusion_failed_count += 1

        original_image_file = card.get("image_file", "") or ""
        occluded_image_file = card.get("occluded_image_file", "") or ""
        media_file = occluded_image_file if occlusion_applied else original_image_file
        front_html = card.get("front", "")
        if requires_image and media_file:
            front_html = f'<img src="{media_file}"><br>{front_html}'

        anki_ready = bool(card.get("anki_ready", True))
        if anki_ready:
            anki_ready_count += 1
        else:
            quality_review_count += 1

        qa_rows.append([
            f"C{idx:05d}", card.get("source_fact_id", ""), card.get("front", ""),
            card.get("back", ""), tags, source,
            card.get("question_type", "text_only"), requires_image,
            card.get("image_region_id", ""), csv_value(card.get("crop_bbox_norm")),
            csv_value(card.get("crop_bbox_px")), card.get("image_path", ""),
            bool(card.get("occlusion_candidate", False)), card.get("occlusion_target", "") or "",
            occlusion_applied, occlusion_status,
            csv_value(card.get("occlusion_bbox_norm")),
            csv_value(card.get("occlusion_bbox_px")),
            card.get("occluded_image_path", "") or "",
            card.get("visual_dependency", "none"),
            card.get("image_decision_reason", "") or "",
            card.get("image_validation_status", "") or "",
            card.get("quality_status", "ok") or "ok",
            csv_value(card.get("quality_flags", [])),
            anki_ready,
        ])

        if anki_ready:
            anki_rows.append([
                front_html, card.get("back", ""), tags, source,
                requires_image, media_file, card.get("question_type", "text_only"),
                occlusion_applied, card.get("occlusion_target", "") or "",
            ])
        else:
            review_reason_parts = []
            if occlusion_status not in {"not_requested", "applied"}:
                review_reason_parts.append(occlusion_status)
            review_reason_parts.extend(card.get("quality_flags", []) or [])
            review_rows.append([
                f"C{idx:05d}", card.get("front", ""), card.get("back", ""),
                card.get("question_type", "text_only"), " / ".join(review_reason_parts) or "要確認",
                card.get("image_region_id", ""), card.get("occlusion_target", "") or "",
                card.get("image_path", "") or "", source,
            ])

    prefix = safe_name(pdf_path.stem)
    write_csv(output_dir / f"{prefix}_01_ページ内容確認.csv",
              ["Page", "Sections", "ContentReview", "VisualReview"], page_review_rows)
    write_csv(output_dir / f"{prefix}_01b_画像領域.csv",
              ["ImageRegionID", "Page", "RegionType", "Description", "CropBBoxNorm",
               "CropBBoxPx", "ImagePath", "UsableForQuestion"], region_rows)
    write_csv(output_dir / f"{prefix}_02_情報抽出.csv",
              ["FactID", "Category", "ExtractedFact", "SourceType", "Page",
               "ImageRegionID", "VisualDependency", "Subject", "Relation", "Object",
               "EvidenceBBoxNorm", "EvidenceBBoxPx", "EvidenceImagePath", "RegionImagePath"], fact_rows)
    write_csv(output_dir / f"{prefix}_03_重要事項選定.csv",
              ["FactID", "Importance", "Selected", "Reason", "Page", "RequiresImageCandidate",
               "ImageRegionID", "EvidenceBBoxNorm", "ImagePath"], importance_rows)
    write_csv(output_dir / f"{prefix}_04_一問一答化.csv",
              ["CardID", "SourceFactID", "Front", "Back", "Tags", "Source", "QuestionType",
               "RequiresImage", "ImageRegionID", "CropBBoxNorm", "CropBBoxPx", "ImagePath",
               "OcclusionCandidate", "OcclusionTarget", "OcclusionApplied", "OcclusionStatus",
               "OcclusionBBoxNorm", "OcclusionBBoxPx", "OccludedImagePath",
               "VisualDependency", "ImageDecisionReason", "ImageValidationStatus",
               "QualityStatus", "QualityFlags", "AnkiReady"], qa_rows)
    write_csv(output_dir / f"{prefix}_05_Anki.csv",
              ["Front", "Back", "Tags", "Source", "RequiresImage", "MediaFile",
               "QuestionType", "OcclusionApplied", "OcclusionTarget"], anki_rows)
    write_csv(output_dir / f"{prefix}_05b_要確認カード.csv",
              ["CardID", "Front", "Back", "QuestionType", "Reason", "ImageRegionID",
               "OcclusionTarget", "OriginalImagePath", "Source"], review_rows)

    return {
        "pages": len(page_results),
        "image_regions": len(region_rows),
        "facts": total_facts,
        "selected_facts": selected_facts,
        "cards": len(all_cards),
        "image_cards": image_card_count,
        "occlusion_cards": occlusion_card_count,
        "occlusion_failed": occlusion_failed_count,
        "quality_review": quality_review_count,
        "anki_ready_cards": anki_ready_count,
    }


def _write_resume_state(work_dir: Path, data: dict[str, Any]) -> None:
    state_path = work_dir / "resume_state.json"
    payload = dict(data)
    payload["updated_at_epoch"] = time.time()
    state_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def process_pdf(pdf_path: Path, output_dir: Path, config: Config, dry_run: bool = False,
                start_page: int | None = None, end_page: int | None = None,
                progress_callback: Callable[[int, int, int | None], None] | None = None,
                status_callback: Callable[[str], None] | None = None) -> dict[str, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir = output_dir / "_work"
    cache_dir = work_dir / "cache"
    media_dir = output_dir / "media"
    cache_dir.mkdir(parents=True, exist_ok=True)
    media_dir.mkdir(parents=True, exist_ok=True)

    pages = render_and_extract(pdf_path, work_dir, config.dpi)
    if start_page is not None:
        pages = [p for p in pages if p["page"] >= start_page]
    if end_page is not None:
        pages = [p for p in pages if p["page"] <= end_page]

    if dry_run:
        rows = [[p["page"], len(p["text"]), str(p["image_path"])] for p in pages]
        write_csv(output_dir / "dry_run_pages.csv", ["Page", "ExtractedTextChars", "ImagePath"], rows)
        print(f"Dry run complete: {len(pages)} pages")
        return {
            "pages": len(pages),
            "image_regions": 0,
            "facts": 0,
            "selected_facts": 0,
            "cards": 0,
            "image_cards": 0,
        }

    client = create_ai_client(config.provider)
    page_results: list[dict[str, Any]] = []
    pdf_prefix = safe_name(pdf_path.stem)
    fallback_models = config.fallback_model_list()

    variant_payload = {
        "provider": config.provider,
        "model": config.model,
        "min_importance": config.min_importance,
        "dpi": config.dpi,
        "enable_image_cards": config.enable_image_cards,
        "enable_image_occlusion": config.enable_image_occlusion,
        "quality_pipeline": "v5.3_atomic_diagram_image_binding",
    }
    variant_hash = hashlib.sha256(
        json.dumps(variant_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()[:10]

    if progress_callback:
        progress_callback(0, len(pages), None)

    _write_resume_state(
        work_dir,
        {
            "status": "running",
            "provider": config.provider,
            "model": config.model,
            "variant_hash": variant_hash,
            "total_pages": len(pages),
            "page": None,
            "stage": "start",
        },
    )

    for n, p in enumerate(pages, start=1):
        page_no = p["page"]
        prefix = (
            f"page_{page_no:04d}_v53_"
            f"{safe_name(config.provider)}_{safe_name(config.model)}_{variant_hash}"
        )
        complete_cache = cache_dir / f"{prefix}_complete.json"
        analysis_cache = cache_dir / f"{prefix}_analysis.json"
        qa_cache = cache_dir / f"{prefix}_qa.json"

        # v5.1 complete-page cache compatibility.
        legacy_cache = cache_dir / (
            f"page_{page_no:04d}_v51_{safe_name(config.provider)}_{safe_name(config.model)}.json"
        )

        print(f"[{n}/{len(pages)}] page {page_no}")
        if status_callback:
            status_callback(f"ページ {page_no}: 処理を開始します。")

        if complete_cache.exists():
            page_result = json.loads(complete_cache.read_text(encoding="utf-8"))
            page_results.append(page_result)
            print("  v5.2 complete cache hit")
            if status_callback:
                status_callback(f"ページ {page_no}: 保存済み結果を再利用しました。")
            if progress_callback:
                progress_callback(n, len(pages), page_no)
            continue

        if legacy_cache.exists():
            page_result = json.loads(legacy_cache.read_text(encoding="utf-8"))
            complete_cache.write_text(
                json.dumps(page_result, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            page_results.append(page_result)
            print("  v5.1 complete cache migrated")
            if status_callback:
                status_callback(f"ページ {page_no}: v5.1の成功済み結果を再利用しました。")
            if progress_callback:
                progress_callback(n, len(pages), page_no)
            continue

        try:
            # ----- Stage 1: analysis -----
            _write_resume_state(
                work_dir,
                {
                    "status": "running",
                    "page": page_no,
                    "page_index": n,
                    "total_pages": len(pages),
                    "stage": "analysis",
                    "provider": config.provider,
                    "model": config.model,
                    "variant_hash": variant_hash,
                },
            )

            if analysis_cache.exists():
                analyzed = json.loads(analysis_cache.read_text(encoding="utf-8"))
                if status_callback:
                    status_callback(f"ページ {page_no}: 解析済みチェックポイントを再利用します。")
            else:
                analyzed_raw = call_vision_json(
                    config.provider,
                    client,
                    config.model,
                    ANALYZE_PROMPT,
                    p["text"],
                    p["image_path"],
                    config.max_retries,
                    fallback_models=fallback_models,
                    min_request_interval_seconds=config.min_request_interval_seconds,
                    max_retry_wait_seconds=config.max_retry_wait_seconds,
                    status_callback=status_callback,
                )
                analyzed = normalize_analysis(page_no, analyzed_raw)
                analysis_cache.write_text(
                    json.dumps(analyzed, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

            regions = crop_image_regions(
                p["image_path"],
                analyzed.get("image_regions", []),
                media_dir,
                pdf_prefix=pdf_prefix,
                padding=config.crop_padding,
            )
            facts = crop_required_fact_evidence(
                p["image_path"], analyzed.get("facts", []), media_dir,
                pdf_prefix=pdf_prefix, padding=max(config.crop_padding, 0.05),
            )
            facts = enrich_facts_with_regions(facts, regions)
            selected = [
                f for f in facts
                if int(f.get("importance", 0) or 0) >= config.min_importance
            ]

            # ----- Stage 2: Q&A -----
            _write_resume_state(
                work_dir,
                {
                    "status": "running",
                    "page": page_no,
                    "page_index": n,
                    "total_pages": len(pages),
                    "stage": "qa",
                    "provider": config.provider,
                    "model": config.model,
                    "variant_hash": variant_hash,
                },
            )

            qa_payload = {
                "page": page_no,
                "min_importance": config.min_importance,
                "allow_image_cards": config.enable_image_cards,
                "allow_image_occlusion": config.enable_image_occlusion,
                "facts": selected,
                "image_regions": [
                    {
                        "image_region_id": r.get("image_region_id"),
                        "region_type": r.get("region_type"),
                        "description": r.get("description"),
                        "usable_for_question": r.get("usable_for_question"),
                    }
                    for r in regions
                ],
            }

            if qa_cache.exists():
                qa = json.loads(qa_cache.read_text(encoding="utf-8"))
                if status_callback:
                    status_callback(f"ページ {page_no}: 一問一答チェックポイントを再利用します。")
            else:
                qa = call_text_json(
                    config.provider,
                    client,
                    config.model,
                    QA_PROMPT,
                    qa_payload,
                    config.max_retries,
                    fallback_models=fallback_models,
                    min_request_interval_seconds=config.min_request_interval_seconds,
                    max_retry_wait_seconds=config.max_retry_wait_seconds,
                    status_callback=status_callback,
                )
                qa_cache.write_text(
                    json.dumps(qa, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

            cards = attach_card_region_metadata(
                qa.get("cards", []),
                regions,
                config.enable_image_cards,
                facts=selected,
            )

            # ----- Stage 3: image occlusion -----
            _write_resume_state(
                work_dir,
                {
                    "status": "running",
                    "page": page_no,
                    "page_index": n,
                    "total_pages": len(pages),
                    "stage": "image_occlusion",
                    "provider": config.provider,
                    "model": config.model,
                    "variant_hash": variant_hash,
                },
            )

            cards = apply_image_occlusions(
                provider=config.provider,
                client=client,
                model=config.model,
                cards=cards,
                regions=regions,
                media_dir=media_dir,
                pdf_prefix=pdf_prefix,
                page_no=page_no,
                max_retries=config.max_retries,
                enabled=config.enable_image_cards and config.enable_image_occlusion,
                padding=config.occlusion_padding,
                fallback_models=fallback_models,
                min_request_interval_seconds=config.min_request_interval_seconds,
                max_retry_wait_seconds=config.max_retry_wait_seconds,
                status_callback=status_callback,
            )

            page_result = {
                "page": page_no,
                "page_summary": analyzed.get("page_summary", {}),
                "image_regions": regions,
                "facts": facts,
                "cards": cards,
            }
            complete_cache.write_text(
                json.dumps(page_result, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            page_results.append(page_result)

            _write_resume_state(
                work_dir,
                {
                    "status": "running",
                    "page": page_no,
                    "page_index": n,
                    "total_pages": len(pages),
                    "stage": "page_complete",
                    "provider": config.provider,
                    "model": config.model,
                    "variant_hash": variant_hash,
                },
            )

            if progress_callback:
                progress_callback(n, len(pages), page_no)
            if status_callback:
                status_callback(f"ページ {page_no}: 完了・保存しました。")
            time.sleep(config.sleep_seconds)

        except Exception as exc:
            _write_resume_state(
                work_dir,
                {
                    "status": "paused",
                    "page": page_no,
                    "page_index": n,
                    "total_pages": len(pages),
                    "stage": "failed",
                    "provider": config.provider,
                    "model": config.model,
                    "variant_hash": variant_hash,
                    "error": str(exc),
                },
            )
            if status_callback:
                status_callback(
                    f"ページ {page_no} で一時停止しました。再開時は成功済みの段階から続行します。"
                )
            raise

    summary = build_outputs(pdf_path, output_dir, page_results, config.min_importance)

    _write_resume_state(
        work_dir,
        {
            "status": "complete",
            "provider": config.provider,
            "model": config.model,
            "variant_hash": variant_hash,
            "total_pages": len(pages),
            "page": pages[-1]["page"] if pages else None,
            "stage": "complete",
            "summary": summary,
        },
    )

    print(f"Complete: {output_dir}")
    print(json.dumps(summary, ensure_ascii=False))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="講義PDFを解析し、文章/画像一問一答とAnki CSVを生成するPoC")
    parser.add_argument("pdf", type=Path, help="入力PDF")
    parser.add_argument("--out", type=Path, default=Path("output"), help="出力フォルダ")
    parser.add_argument(
        "--provider",
        choices=["openai", "gemini"],
        default=os.getenv("AI_PROVIDER", "gemini"),
        help="生成AIプロバイダ",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="モデルID。省略時はproviderごとの環境変数/既定値を使用",
    )
    parser.add_argument("--min-importance", type=int, default=3, choices=range(1, 6))
    parser.add_argument("--dpi", type=int, default=160)
    parser.add_argument("--start-page", type=int)
    parser.add_argument("--end-page", type=int)
    parser.add_argument("--no-image-cards", action="store_true", help="画像依存カードを生成しない")
    parser.add_argument("--no-image-occlusion", action="store_true", help="図中ラベルを隠すImage Occlusionを生成しない")
    parser.add_argument(
        "--fallback-models",
        default=os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash-lite"),
        help="Geminiのフォールバックモデル。複数はカンマ区切り",
    )
    parser.add_argument(
        "--min-request-interval",
        type=float,
        default=float(os.getenv("GEMINI_MIN_REQUEST_INTERVAL_SECONDS", "15")),
        help="Gemini API呼び出しの最小間隔（秒）。無料枠では15秒程度を推奨",
    )
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true", help="APIを呼ばずPDF処理だけ確認")
    args = parser.parse_args()

    config = Config(
        provider=args.provider,
        model=args.model or default_model(args.provider),
        min_importance=args.min_importance,
        dpi=args.dpi,
        enable_image_cards=not args.no_image_cards,
        enable_image_occlusion=not args.no_image_occlusion,
        fallback_models=args.fallback_models,
        min_request_interval_seconds=args.min_request_interval,
        max_retries=args.max_retries,
    )
    process_pdf(args.pdf, args.out, config, args.dry_run, args.start_page, args.end_page)


if __name__ == "__main__":
    main()
