# CHANGELOG

## v5.3
- 1カード1知識の厳格化
- 複合カード自動検出
- visual_relationsによる模式図網羅抽出
- 図由来と画像必須判定の分離
- fact固有evidence_bbox/crop
- 画像カードと質問の対応強化
- QualityStatus / QualityFlags / ImageValidationStatus追加
- v5.3専用キャッシュ

## v5.2
- Gemini無料枠向けAPI pacing
- 429 RetryInfo / retryDelay対応
- 503指数バックオフ
- Gemini fallback model
- analysis / QA段階別チェックポイント
- 完了ページキャッシュ
- v5.1成功済みページキャッシュ再利用
- resume_state.json
- pausedプロジェクトの再開ボタン
- 一時エラー時のユーザー向け表示改善

## v5.1
- OpenAI / Gemini切替
- google-genai対応
- Gemini画像入力対応
- Gemini JSON応答モード
- Provider/Modelをプロジェクト設定へ保存
- Provider/Modelごとのキャッシュ分離
- CLIに--provider追加

## v5
- ユーザー登録 / ログイン
- PBKDF2-SHA256パスワードハッシュ
- SQLite DB
- users / projects / cards
- プロジェクト一覧
- PDF / media / cards 永続保存
- 後日編集再開
- DBへの編集保存
- プロジェクト削除

## v4
- Streamlit UIを4タブ構成に変更
- 生成進捗表示
- カード確認・編集
- 採用/除外
- 画像カードプレビュー
- Image Occlusion確認
- 編集済みAnki CSV生成
- 解析結果 + mediaをZIP出力

## v3
- Image Occlusion

## v2
- 画像領域/crop/requires_image

## v1
- PDF→一問一答→CSV
