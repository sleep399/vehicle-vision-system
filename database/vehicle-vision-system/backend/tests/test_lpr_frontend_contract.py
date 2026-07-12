from pathlib import Path


STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


def test_lpr_frontend_keeps_batch_and_rtsp_controls():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (STATIC_DIR / "js" / "app.js").read_text(encoding="utf-8")

    assert 'id="lpr-file" accept="image/*,video/*" multiple' in html
    assert 'id="lpr-batch-nav"' in html
    assert 'id="lpr-rtsp-video"' in html
    assert "renderLprBatchResults(batchResults" in js
    assert "videoFiles.length > 0 && files.length > 1" in js
    assert "bindPoliceImageViewer()" in js
