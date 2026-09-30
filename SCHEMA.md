# v2 data schema

## 02_情報抽出.csv

| Column | Meaning |
|---|---|
| FactID | ページ内で一意な知識ID |
| Category | 解剖・生理・診断など |
| ExtractedFact | 抽出された独立知識 |
| SourceType | text / figure / table / mixed |
| Page | 元PDFページ |
| ImageRegionID | 関連画像領域ID。なければ空欄 |
| VisualDependency | none / helpful / required |
| CropBBoxNorm | 0〜1000正規化座標 `[x1,y1,x2,y2]` |
| CropBBoxPx | レンダリング画像上のピクセル座標 |
| ImagePath | crop画像の相対パス |

## 03_重要事項選定.csv

`RequiresImageCandidate=True` は、そのfactが画像なしでは成立しにくい候補であることを示します。
最終的な `RequiresImage` はカード生成時に判定します。

## 04_一問一答化.csv

| Column | Meaning |
|---|---|
| QuestionType | text_only / image_based / hybrid |
| RequiresImage | 画像がないとカードが成立しないか |
| ImageRegionID | 使用するcrop領域 |
| CropBBoxNorm | 正規化crop座標 |
| CropBBoxPx | ピクセルcrop座標 |
| ImagePath | crop画像パス |

## 05_Anki.csv

画像カードでは `Front` に `<img src="画像ファイル名">` を挿入します。
`media/` 内の画像をAnki側のメディアへ配置すると表示できます。
