
from __future__ import annotations

from pathlib import Path
import io
import json
import os
import shutil
import zipfile

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from db import (
    init_db,
    create_user,
    authenticate_user,
    create_project,
    update_project,
    list_projects,
    get_project,
    delete_project,
    project_dir,
    import_cards_dataframe,
    load_cards_dataframe,
    save_edited_cards,
)
from pipeline import Config, process_pdf, safe_name, default_model

load_dotenv()
init_db()

st.set_page_config(
    page_title="Lecture2Anki v5.3",
    page_icon="🧠",
    layout="wide",
)

# ---------- common helpers ----------
def as_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if pd.isna(v):
        return False
    return str(v).strip().lower() in {"true", "1", "yes", "y", "採用"}

def clean_text(v) -> str:
    if pd.isna(v):
        return ""
    return str(v)

def find_output_csv(output_dir: Path, suffix: str) -> Path:
    candidates = list(output_dir.glob(f"*{suffix}"))
    if not candidates:
        raise FileNotFoundError(f"{suffix} が見つかりません。")
    return candidates[0]

def read_cards_from_pipeline(output_dir: Path) -> pd.DataFrame:
    p = find_output_csv(output_dir, "_04_一問一答化.csv")
    df = pd.read_csv(p, encoding="utf-8-sig")
    if "AnkiReady" not in df.columns:
        df["AnkiReady"] = True
    if "採用" not in df.columns:
        df.insert(0, "採用", df["AnkiReady"].map(as_bool))
    return df

def card_media_file(row: pd.Series) -> str:
    occ = clean_text(row.get("OccludedImagePath", ""))
    original = clean_text(row.get("ImagePath", ""))
    chosen = occ if as_bool(row.get("OcclusionApplied", False)) and occ else original
    return Path(chosen).name if chosen else ""

def build_anki_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, row in df.iterrows():
        if not as_bool(row.get("採用", False)):
            continue
        front = clean_text(row.get("Front", ""))
        back = clean_text(row.get("Back", ""))
        tags = clean_text(row.get("Tags", ""))
        source = clean_text(row.get("Source", ""))
        requires_image = as_bool(row.get("RequiresImage", False))
        media_file = card_media_file(row)

        if requires_image and media_file:
            front = f'<img src="{media_file}"><br>{front}'

        rows.append({
            "Front": front,
            "Back": back,
            "Tags": tags,
            "Source": source,
            "RequiresImage": requires_image,
            "MediaFile": media_file,
            "QuestionType": clean_text(row.get("QuestionType", "text_only")),
            "OcclusionApplied": as_bool(row.get("OcclusionApplied", False)),
            "OcclusionTarget": clean_text(row.get("OcclusionTarget", "")),
        })
    return pd.DataFrame(rows)

def df_to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")

def build_export_zip(output_dir: Path, edited_df: pd.DataFrame) -> bytes:
    anki_df = build_anki_dataframe(edited_df)
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("edited_cards.csv", df_to_csv_bytes(edited_df))
        zf.writestr("anki_edited.csv", df_to_csv_bytes(anki_df))

        for p in sorted(output_dir.glob("*.csv")):
            zf.write(p, arcname=f"pipeline/{p.name}")

        media_dir = output_dir / "media"
        if media_dir.exists():
            for p in sorted(media_dir.glob("*")):
                if p.is_file():
                    zf.write(p, arcname=f"media/{p.name}")

    bio.seek(0)
    return bio.getvalue()

def logout():
    for key in list(st.session_state.keys()):
        if key.startswith("auth_") or key.startswith("current_") or key == "cards_df":
            st.session_state.pop(key, None)
    st.rerun()

def open_project(pid: int):
    user = st.session_state["auth_user"]
    project = get_project(pid, user["id"])
    if not project:
        st.error("プロジェクトが見つかりません。")
        return
    st.session_state["current_project_id"] = pid
    st.session_state["current_project"] = project
    st.session_state["cards_df"] = load_cards_dataframe(pid)
    st.session_state["editor_version"] = st.session_state.get("editor_version", 0) + 1
    st.rerun()

# ---------- authentication ----------
if "auth_user" not in st.session_state:
    st.title("🧠 Lecture2Anki v5.3")
    st.caption("ローカルβ版：ユーザー登録・ログイン・プロジェクト保存・後日編集")

    login_tab, register_tab = st.tabs(["ログイン", "新規登録"])

    with login_tab:
        with st.form("login_form"):
            email = st.text_input("メールアドレス")
            password = st.text_input("パスワード", type="password")
            submitted = st.form_submit_button("ログイン", type="primary", use_container_width=True)
        if submitted:
            user = authenticate_user(email, password)
            if user:
                st.session_state["auth_user"] = user
                st.rerun()
            else:
                st.error("メールアドレスまたはパスワードが違います。")

    with register_tab:
        with st.form("register_form"):
            email = st.text_input("メールアドレス", key="register_email")
            password = st.text_input("パスワード（8文字以上）", type="password", key="register_password")
            confirm = st.text_input("パスワード確認", type="password")
            submitted = st.form_submit_button("ユーザー登録", use_container_width=True)
        if submitted:
            if password != confirm:
                st.error("パスワードが一致しません。")
            else:
                ok, message = create_user(email, password)
                if ok:
                    st.success(message + " 「ログイン」タブからログインしてください。")
                else:
                    st.error(message)

    st.info(
        "v5のログインはローカルPC内のSQLite用です。"
        "公開WebサービスではSupabase/Auth0/Clerk等の認証基盤へ置き換える想定です。"
    )
    st.stop()

user = st.session_state["auth_user"]

# ---------- sidebar ----------
with st.sidebar:
    st.markdown(f"### 👤 {user['email']}")
    if st.button("ログアウト", use_container_width=True):
        logout()

    st.divider()
    st.header("AI設定")

    provider = st.selectbox(
        "AI Provider",
        ["gemini", "openai"],
        index=0 if os.getenv("AI_PROVIDER", "gemini").lower() == "gemini" else 1,
        format_func=lambda x: "Google Gemini" if x == "gemini" else "OpenAI",
    )

    if provider == "gemini":
        if os.getenv("GEMINI_API_KEY"):
            st.success("Gemini APIキー読込済み")
        api_key = st.text_input(
            "Gemini API key",
            type="password",
            placeholder=".env設定済みなら空欄",
            key="gemini_api_key_input",
        )
        model = st.text_input(
            "Gemini model",
            value=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
            key="gemini_model_input",
        )
    else:
        if os.getenv("OPENAI_API_KEY"):
            st.success("OpenAI APIキー読込済み")
        api_key = st.text_input(
            "OpenAI API key",
            type="password",
            placeholder=".env設定済みなら空欄",
            key="openai_api_key_input",
        )
        model = st.text_input(
            "OpenAI model",
            value=os.getenv("OPENAI_MODEL", "gpt-5.6-luna"),
            key="openai_model_input",
        )

    # 入力されたキーは新規作成だけでなく「再開」にも使えるよう、その場で環境変数へ反映。
    if api_key:
        if provider == "gemini":
            os.environ["GEMINI_API_KEY"] = api_key
        else:
            os.environ["OPENAI_API_KEY"] = api_key

    st.caption(f"使用予定: {provider} / {model}")

    if provider == "gemini":
        st.markdown("**Gemini安定化設定**")
        free_tier_mode = st.checkbox(
            "無料枠モード（API呼び出し間隔を空ける）",
            value=True,
            help="429を避けるため、API呼び出しを約15秒以上空けます。",
        )
        min_request_interval_seconds = st.number_input(
            "最小API間隔（秒）",
            min_value=0.0,
            max_value=120.0,
            value=15.0 if free_tier_mode else 0.0,
            step=1.0,
            disabled=free_tier_mode,
        )
        if free_tier_mode:
            min_request_interval_seconds = 15.0

        fallback_models = st.text_input(
            "503/429時のフォールバックモデル",
            value=os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash-lite"),
            help="複数指定する場合はカンマ区切り。空欄なら自動切替しません。",
        )
    else:
        free_tier_mode = False
        min_request_interval_seconds = 0.0
        fallback_models = ""

    max_retries = st.slider(
        "一時エラーの最大試行回数（各モデル）",
        min_value=1,
        max_value=5,
        value=3,
    )

    importance = st.slider("最低重要度", 1, 5, 3)
    dpi = st.select_slider("ページ画像DPI", [120, 160, 200, 240], 160)
    enable_image_cards = st.checkbox("画像カード", value=True)
    enable_image_occlusion = st.checkbox(
        "Image Occlusion",
        value=True,
        disabled=not enable_image_cards,
    )


def resume_saved_project(pid: int) -> None:
    project = get_project(pid, user["id"])
    if not project:
        st.error("プロジェクトが見つかりません。")
        return

    try:
        settings = json.loads(project.get("settings_json") or "{}")
    except Exception:
        settings = {}

    saved_provider = settings.get("provider", "gemini")
    required_key = "GEMINI_API_KEY" if saved_provider == "gemini" else "OPENAI_API_KEY"
    if not os.getenv(required_key):
        st.error(
            f"{required_key} がありません。左側で {saved_provider} を選び、APIキーを入力するか、"
            ".envに設定してください。"
        )
        return

    pdir = project_dir(pid)
    pdf_path = pdir / "source" / project["source_filename"]
    out_dir = pdir / "output"
    if not pdf_path.exists():
        st.error(f"元PDFが見つかりません: {pdf_path}")
        return

    progress = st.progress(0.0, text="再開準備中…")
    status_box = st.empty()

    def progress_cb(done: int, total: int, page_no: int | None):
        ratio = 0 if total == 0 else done / total
        msg = "再開準備中…" if page_no is None else f"ページ {page_no} ({done}/{total})"
        progress.progress(ratio, text=msg)

    cfg = Config(
        provider=saved_provider,
        model=settings.get("model") or default_model(saved_provider),
        min_importance=int(settings.get("min_importance", 3)),
        dpi=int(settings.get("dpi", 160)),
        enable_image_cards=bool(settings.get("enable_image_cards", True)),
        enable_image_occlusion=bool(settings.get("enable_image_occlusion", True)),
        fallback_models=settings.get(
            "fallback_models",
            os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash-lite"),
        ),
        min_request_interval_seconds=float(
            settings.get(
                "min_request_interval_seconds",
                15.0 if saved_provider == "gemini" else 0.0,
            )
        ),
        max_retries=int(settings.get("max_retries", 3)),
    )

    update_project(pid, status="processing")

    try:
        summary = process_pdf(
            pdf_path,
            out_dir,
            cfg,
            start_page=settings.get("start_page"),
            end_page=settings.get("end_page"),
            progress_callback=progress_cb,
            status_callback=lambda message: status_box.caption(message),
        )

        cards_df = read_cards_from_pipeline(out_dir)
        import_cards_dataframe(pid, cards_df)
        update_project(pid, status="complete", summary=summary)

        st.session_state["current_project_id"] = pid
        st.session_state["current_project"] = get_project(pid, user["id"])
        st.session_state["cards_df"] = load_cards_dataframe(pid)
        st.session_state["editor_version"] = st.session_state.get("editor_version", 0) + 1

        progress.progress(1.0, text="再開処理が完了しました。")
        st.success("処理が完了しました。成功済みチェックポイントは再利用されています。")
    except Exception as exc:
        update_project(pid, status="paused")
        st.error(
            "再度一時停止しました。チェックポイントは残っているので、時間を空けて再開できます。"
        )
        st.code(str(exc))


st.title("🧠 Lecture2Anki v5.3")
st.caption("PDF → OpenAI/Gemini → 図表網羅・画像整合 → 自動待機・途中再開 → DB保存 → Anki出力")

tabs = st.tabs(["🏠 プロジェクト", "➕ 新規PDF", "✏️ カード編集", "🖼️ 画像確認", "📦 出力"])

# ---------- projects ----------
with tabs[0]:
    st.subheader("保存済みプロジェクト")
    projects = list_projects(user["id"])

    if not projects:
        st.info("まだプロジェクトがありません。「➕ 新規PDF」から作成してください。")
    else:
        for p in projects:
            with st.container(border=True):
                c1, c2, c3, c4 = st.columns([3.2, 1.3, 1.2, 1.4])
                with c1:
                    st.markdown(f"### {p['name']}")
                    st.caption(
                        f"{p['source_filename']} / 状態: {p['status']} / 更新: {p['updated_at'][:19]}"
                    )
                with c2:
                    st.metric("カード", p.get("card_count") or 0)
                with c3:
                    st.metric("採用", p.get("accepted_count") or 0)
                with c4:
                    if p["status"] in {"paused", "error", "processing"}:
                        if st.button("▶ 再開", key=f"resume_{p['id']}", use_container_width=True):
                            resume_saved_project(int(p["id"]))
                    if st.button("開く", key=f"open_{p['id']}", use_container_width=True):
                        open_project(int(p["id"]))
                    if st.button("削除", key=f"delete_{p['id']}", use_container_width=True):
                        if delete_project(int(p["id"]), user["id"]):
                            if st.session_state.get("current_project_id") == int(p["id"]):
                                st.session_state.pop("current_project_id", None)
                                st.session_state.pop("current_project", None)
                                st.session_state.pop("cards_df", None)
                            st.rerun()

    if st.session_state.get("current_project"):
        cp = st.session_state["current_project"]
        st.success(f"現在編集中: {cp['name']}")

# ---------- new PDF ----------
with tabs[1]:
    st.subheader("新しいPDFからプロジェクトを作成")

    uploaded = st.file_uploader("講義PDF", type=["pdf"])
    default_name = Path(uploaded.name).stem if uploaded else ""
    project_name = st.text_input("プロジェクト名", value=default_name)

    c1, c2 = st.columns(2)
    with c1:
        start_page = st.number_input("開始ページ（0=先頭）", min_value=0, value=0, step=1)
    with c2:
        end_page = st.number_input("終了ページ（0=末尾）", min_value=0, value=0, step=1)

    if st.button("プロジェクト作成・解析開始", type="primary", disabled=uploaded is None):
        if api_key:
            if provider == "gemini":
                os.environ["GEMINI_API_KEY"] = api_key
            else:
                os.environ["OPENAI_API_KEY"] = api_key

        required_key = "GEMINI_API_KEY" if provider == "gemini" else "OPENAI_API_KEY"
        if not os.getenv(required_key):
            st.error(f"{required_key} がありません。")
        elif not project_name.strip():
            st.error("プロジェクト名を入力してください。")
        else:
            settings = {
                "provider": provider,
                "model": model,
                "min_importance": importance,
                "dpi": dpi,
                "enable_image_cards": enable_image_cards,
                "enable_image_occlusion": enable_image_cards and enable_image_occlusion,
                "fallback_models": fallback_models,
                "min_request_interval_seconds": min_request_interval_seconds,
                "max_retries": max_retries,
                "start_page": int(start_page) if start_page else None,
                "end_page": int(end_page) if end_page else None,
            }
            pid = create_project(
                user["id"],
                project_name.strip(),
                uploaded.name,
                settings,
            )
            pdir = project_dir(pid)
            pdf_dir = pdir / "source"
            out_dir = pdir / "output"
            pdf_dir.mkdir(exist_ok=True)
            out_dir.mkdir(exist_ok=True)
            pdf_path = pdf_dir / uploaded.name
            pdf_path.write_bytes(uploaded.getvalue())

            progress = st.progress(0.0, text="準備中…")
            status = st.empty()

            def progress_cb(done: int, total: int, page_no: int | None):
                ratio = 0 if total == 0 else done / total
                msg = "PDFを準備中…" if page_no is None else f"ページ {page_no} ({done}/{total})"
                progress.progress(ratio, text=msg)
                status.caption(msg)

            try:
                summary = process_pdf(
                    pdf_path,
                    out_dir,
                    Config(
                        provider=provider,
                        model=model,
                        min_importance=importance,
                        dpi=dpi,
                        enable_image_cards=enable_image_cards,
                        enable_image_occlusion=enable_image_cards and enable_image_occlusion,
                        fallback_models=fallback_models,
                        min_request_interval_seconds=min_request_interval_seconds,
                        max_retries=max_retries,
                    ),
                    start_page=int(start_page) if start_page else None,
                    end_page=int(end_page) if end_page else None,
                    progress_callback=progress_cb,
                    status_callback=lambda message: status.caption(message),
                )

                cards_df = read_cards_from_pipeline(out_dir)
                import_cards_dataframe(pid, cards_df)
                update_project(pid, status="complete", summary=summary)

                st.session_state["current_project_id"] = pid
                st.session_state["current_project"] = get_project(pid, user["id"])
                st.session_state["cards_df"] = load_cards_dataframe(pid)
                st.session_state["editor_version"] = st.session_state.get("editor_version", 0) + 1

                progress.progress(1.0, text="完了")
                st.success("解析結果をDBに保存しました。後日ログインして続きから編集できます。")
            except Exception as e:
                update_project(pid, status="paused")
                st.error(
                    "処理を一時停止しました。成功済みのページ・解析段階は保存されています。"
                    "「🏠 プロジェクト」から「▶ 再開」を押してください。"
                )
                st.code(str(e))

# ---------- editor ----------
with tabs[2]:
    st.subheader("カード確認・編集")

    pid = st.session_state.get("current_project_id")
    if not pid:
        st.info("「🏠 プロジェクト」で編集するプロジェクトを開いてください。")
    else:
        project = get_project(pid, user["id"])
        df = st.session_state.get("cards_df")
        if df is None:
            df = load_cards_dataframe(pid)
            st.session_state["cards_df"] = df

        st.markdown(f"**プロジェクト:** {project['name']}")
        st.caption("v5.3: 1カード1知識、図表関係の網羅抽出、画像必須判定、質問とcropの対応を厳格化。")
        total = len(df)
        accepted = int(df["採用"].map(as_bool).sum()) if total else 0
        c1, c2 = st.columns(2)
        c1.metric("全カード", total)
        c2.metric("採用", accepted)

        if total:
            editable = ["採用", "Front", "Back", "Tags"]
            disabled_cols = [c for c in df.columns if c not in editable]

            edited = st.data_editor(
                df,
                key=f"editor_{pid}_{st.session_state.get('editor_version', 0)}",
                use_container_width=True,
                height=560,
                hide_index=True,
                disabled=disabled_cols,
                column_config={
                    "採用": st.column_config.CheckboxColumn("採用"),
                    "Front": st.column_config.TextColumn("問題", width="large"),
                    "Back": st.column_config.TextColumn("答え", width="large"),
                    "Tags": st.column_config.TextColumn("Tags", width="medium"),
                },
            )
            st.session_state["cards_df"] = edited

            save_col, reload_col = st.columns(2)
            with save_col:
                if st.button("💾 編集内容をDBに保存", type="primary", use_container_width=True):
                    save_edited_cards(pid, edited)
                    st.success("保存しました。ブラウザを閉じても次回続きから編集できます。")
            with reload_col:
                if st.button("DBの保存内容を再読込", use_container_width=True):
                    st.session_state["cards_df"] = load_cards_dataframe(pid)
                    st.session_state["editor_version"] = st.session_state.get("editor_version", 0) + 1
                    st.rerun()

            st.divider()
            st.markdown("#### 1枚ずつ確認")
            options = edited["CardID"].astype(str).tolist()
            selected = st.selectbox("CardID", options)
            row = edited.loc[edited["CardID"].astype(str) == selected].iloc[0]

            left, right = st.columns([1.1, 1])
            with left:
                st.markdown("**問題**")
                st.info(clean_text(row.get("Front", "")))
                st.markdown("**答え**")
                st.success(clean_text(row.get("Back", "")))
                st.caption(
                    f"{clean_text(row.get('QuestionType', ''))} / "
                    f"Source: {clean_text(row.get('Source', ''))}"
                )

            with right:
                media_file = card_media_file(row)
                output_dir = project_dir(pid) / "output"
                path = output_dir / "media" / media_file if media_file else None
                if path and path.exists():
                    st.image(str(path), use_container_width=True)
                else:
                    st.caption("画像なし")

# ---------- image review ----------
with tabs[3]:
    st.subheader("画像カード / Image Occlusion")

    pid = st.session_state.get("current_project_id")
    if not pid:
        st.info("プロジェクトを開いてください。")
    else:
        df = st.session_state.get("cards_df", load_cards_dataframe(pid))
        if "RequiresImage" in df.columns:
            img_df = df[df["RequiresImage"].map(as_bool)]
        else:
            img_df = df.iloc[0:0]

        if img_df.empty:
            st.info("画像カードはありません。")
        else:
            output_dir = project_dir(pid) / "output"
            st.write(f"{len(img_df)}件")
            for _, row in img_df.head(30).iterrows():
                with st.container(border=True):
                    c1, c2 = st.columns([1, 1.4])
                    with c1:
                        media = card_media_file(row)
                        p = output_dir / "media" / media if media else None
                        if p and p.exists():
                            st.image(str(p), use_container_width=True)
                        else:
                            st.warning("画像なし")
                    with c2:
                        st.markdown(f"**{clean_text(row.get('CardID',''))}**")
                        st.write(clean_text(row.get("Front", "")))
                        st.success(clean_text(row.get("Back", "")))
                        st.caption(
                            f"Type: {clean_text(row.get('QuestionType',''))} / "
                            f"Occlusion: {clean_text(row.get('OcclusionApplied',''))}"
                        )

# ---------- export ----------
with tabs[4]:
    st.subheader("Anki出力")

    pid = st.session_state.get("current_project_id")
    if not pid:
        st.info("プロジェクトを開いてください。")
    else:
        project = get_project(pid, user["id"])
        df = st.session_state.get("cards_df", load_cards_dataframe(pid))

        if st.button("出力前に現在の編集内容を保存", use_container_width=True):
            save_edited_cards(pid, df)
            st.success("DBへ保存しました。")

        anki_df = build_anki_dataframe(df)
        st.metric("最終採用カード", len(anki_df))
        if len(anki_df):
            st.dataframe(anki_df.head(20), use_container_width=True, hide_index=True)

        prefix = safe_name(project["name"])
        st.download_button(
            "編集済みAnki CSV",
            data=df_to_csv_bytes(anki_df),
            file_name=f"{prefix}_Anki.csv",
            mime="text/csv",
            use_container_width=True,
        )
        st.download_button(
            "全カードCSV",
            data=df_to_csv_bytes(df),
            file_name=f"{prefix}_all_cards.csv",
            mime="text/csv",
            use_container_width=True,
        )

        output_dir = project_dir(pid) / "output"
        zip_bytes = build_export_zip(output_dir, df)
        st.download_button(
            "CSV + media + 解析結果 ZIP",
            data=zip_bytes,
            file_name=f"{prefix}_result.zip",
            mime="application/zip",
            type="primary",
            use_container_width=True,
        )

        st.caption(
            "画像カードをAnkiへ読み込む場合は、ZIPのmediaファイルをAnkiのcollection.mediaへ配置してください。"
        )
