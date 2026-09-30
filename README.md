# Lecture2Anki v5.3 — 医学カード品質改善版

v5.2の429/503耐性・途中再開を維持しつつ、実際の講義PDFと生成CSVの評価で見つかった品質問題を修正した版です。

## v5.3の改善点

- **1カード = 1知識**を厳格化。合成場所＋経路、所属＋機能などの複合カードを禁止。残った場合は要確認へ送ります。
- **模式図の網羅抽出**。`visual_relations` で矢印・抑制・標的臓器・作用を関係単位に分解します。
- **図由来 ≠ 画像必須**。GnRH→LH/FSH、TRH→TSHなど文章だけで問える関係は `text_only / RequiresImage=false` に自動修正します。
- **fact固有crop**。画像認識が本当に必要なfactには `evidence_bbox` を要求し、その範囲を画像カードに優先使用します。
- **品質フラグ**。`VisualDependency`, `ImageDecisionReason`, `ImageValidationStatus`, `QualityStatus`, `QualityFlags` をCSVへ追加しました。
- **v5.3専用キャッシュ**。旧バージョンの解析結果を誤って流用しません。

## v5.2からの移行

`.env` と `data/` はコピーできます。ただし今回の品質改善を反映したいPDFは、**新規プロジェクトとして再解析**してください。

## 起動

```powershell
cd C:\...\pdf_to_anki_poc_v5_3
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
& .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run app.py
```

Gemini無料枠では、まず1ページ・Image Occlusion OFFで品質を確認し、その後ONにするのがおすすめです。
