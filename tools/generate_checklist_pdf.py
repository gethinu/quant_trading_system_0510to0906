import os
import tempfile
import urllib.request

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


def make_checklist(path: str) -> None:
    # Ensure a Japanese-capable TTF is registered with reportlab
    font_name = "NotoSansCJK"
    registered = False
    for candidate in [
        # common Windows fonts (forward slashes resolve to the same files on
        # Windows and match the convention used in apply_title_to_image.py)
        r"C:/Windows/Fonts/meiryo.ttc",
        r"C:/Windows/Fonts/meiryo.ttf",
        r"C:/Windows/Fonts/YuGothR.ttc",
        # common Noto path if present
        r"/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    ]:
        if os.path.exists(candidate):
            try:
                pdfmetrics.registerFont(TTFont(font_name, candidate))
                registered = True
                break
            except Exception:
                pass

    # If not found, download a Noto CJK package to a temp file and register
    if not registered:
        try:
            tmpdir = tempfile.gettempdir()
            ttf_path = os.path.join(tmpdir, "NotoSansCJKjp-Regular.otf")
            if not os.path.exists(ttf_path):
                url = (
                    "https://noto-website-2.storage.googleapis.com/pkgs/"
                    "NotoSansCJKjp-hinted.zip"
                )
                zip_path = os.path.join(tmpdir, "noto_jp.zip")
                urllib.request.urlretrieve(url, zip_path)
                import zipfile

                with zipfile.ZipFile(zip_path) as z:
                    # extract a common OTF/TTF inside
                    for name in z.namelist():
                        if name.lower().endswith((".otf", ".ttf")):
                            z.extract(name, tmpdir)
                            extracted = os.path.join(tmpdir, name)
                            os.rename(extracted, ttf_path)
                            break
            pdfmetrics.registerFont(TTFont(font_name, ttf_path))
            registered = True
        except Exception:
            # fallback to Helvetica (will not display Japanese correctly)
            font_name = "Helvetica"

    c = canvas.Canvas(path, pagesize=A4)
    width, height = A4
    margin = 20 * mm

    # Header
    # Title
    title = "Week2: Playwright CI 整備チェックリスト"
    title_size = 18
    c.setFont(font_name, title_size)
    title_w = c.stringWidth(title, font_name, title_size)
    c.drawString((width - title_w) / 2.0, height - margin, title)

    c.setFont(font_name, 11)
    y = height - margin - title_size - 8

    intro = (
        "このチェックリストは、Playwright を使った E2E テストの CI でよく起きる失敗を減らすための"
        "実用項目です。まず上から順に確認し、必要箇所を修正してください。"
    )
    text_width = width - margin * 2
    from reportlab.lib.utils import simpleSplit

    lines = simpleSplit(intro, font_name, 9, text_width)
    for ln in lines:
        c.drawString(margin, y, ln)
        y -= 12

    y -= 6

    checklist = [
        "固定待機(waitForTimeout)を条件待機(waitForSelector/getByRole等)に置き換える",
        "主要セレクタを getByRole / data-testid に変更し、UI依存を減らす",
        "`playwright.config.ts` で viewport / locale / timezone を固定する",
        "CI コンテナに必要な日本語フォントをインストールする Dockerfile を用意する",
        "スクリーンショット比較の閾値と差分ポリシーを決める",
        "並列実行時の共有リソース競合を検出し、問題テストは serial に分離する",
        "テストごとに UUID サフィックス等の一意データを付与する",
        "外部 API は可能な限りモック/フェイクを利用する",
        "失敗時に必要なログ／スクショ収集テンプレを整備する",
        "CI とローカルの依存バージョンをドキュメント化する",
    ]

    c.setFont(font_name, 12)
    c.drawString(margin, y, "チェックリスト")
    y -= 18

    c.setFont(font_name, 11)
    box_size = 8 * mm
    leading = 18
    for i, item in enumerate(checklist, start=1):
        item_text = f"{i}. {item}"
        item_lines = simpleSplit(item_text, font_name, 11, text_width - box_size - 6)

        # draw checkbox square
        c.rect(margin, y - (box_size - 3), box_size, box_size, stroke=1, fill=0)

        # draw the first line next to box
        if item_lines:
            c.drawString(margin + box_size + 6, y, item_lines[0])
            y -= leading
            for ln in item_lines[1:]:
                c.drawString(margin + box_size + 6, y, ln)
                y -= leading
        else:
            c.drawString(margin + box_size + 6, y, "")
            y -= leading

        y -= 4
        if y < 40 * mm:
            c.showPage()
            y = height - margin
            c.setFont(font_name, 11)

    # Footer / usage note
    if y < 60 * mm:
        c.showPage()
        y = height - margin

    # Footer: generation info
    from datetime import datetime

    c.setFont(font_name, 9)
    gen_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    footer = f"Generated: {gen_str}"
    note = "Adapt to your CI environment before use."
    c.drawRightString(width - margin, 24 * mm, footer)
    c.drawRightString(width - margin, 18 * mm, note)

    c.save()


if __name__ == "__main__":
    out = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "docs",
        "downloads",
        "checklist_week2.pdf",
    )
    make_checklist(out)
    print("Wrote:", out)
