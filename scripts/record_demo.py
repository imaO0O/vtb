"""Записывает демо-видео прототипа и скриншоты для презентации — настоящий прогон интерфейса в браузере.

    scripts\\run_chat.bat                       (чат на http://localhost:8501, модели запущены)
    python scripts/record_demo.py               # → docs/demo/pervyy_chek_demo.mp4, docs/screens/*.png
    python scripts/record_demo.py --url http://localhost:8502

Сценарий — docs/demo_script.md: подсказки банка по счёту Ани, источник ответа, вопрос разговорным языком,
честный отказ, Максим у лимита — сравнение с ИП и прогноз, страница «Для банка». Подписи выводятся поверх
страницы, чтобы видео было понятно без звука. Нужен Microsoft Edge (Playwright запускает его как Chromium);
кадры снимаются через DevTools (screencast) и собираются в MP4 программой ffmpeg из пакета imageio-ffmpeg.
"""
import argparse
import base64
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SCREENS = ROOT / "docs" / "screens"
VIDEO = ROOT / "docs" / "demo"
MAX_SECONDS = 118                # конкурс просит видео на 1–2 минуты

CAPTION_JS = """text => {
  let el = document.getElementById('demo-caption');
  if (!el) {
    el = document.createElement('div');
    el.id = 'demo-caption';
    el.style.cssText = 'position:fixed;left:50%;bottom:92px;transform:translateX(-50%);z-index:99999;' +
      'max-width:86%;background:rgba(22,33,59,.93);color:#fff;font:600 21px/1.35 system-ui,sans-serif;' +
      'padding:12px 22px;border-radius:14px;box-shadow:0 6px 24px rgba(0,0,0,.25);text-align:center';
    document.body.appendChild(el);
  }
  el.style.display = text ? 'block' : 'none';
  el.textContent = text;
}"""


SCROLLER_JS = """() => {
  // самый большой прокручиваемый контейнер страницы (у Streamlit это не document)
  let best = document.scrollingElement, size = 0;
  for (const el of document.querySelectorAll('*')) {
    const st = getComputedStyle(el);
    if (/(auto|scroll)/.test(st.overflowY) && el.scrollHeight > el.clientHeight + 10
        && el.clientHeight * el.clientWidth > size) { best = el; size = el.clientHeight * el.clientWidth; }
  }
  return best;
}"""


class Demo:
    def __init__(self, page, pause: float):
        self.page, self.pause = page, pause

    def caption(self, text: str, seconds: float | None = None) -> None:
        self.page.evaluate(CAPTION_JS, text)
        self.page.wait_for_timeout(int(1000 * (self.pause if seconds is None else seconds)))

    def idle(self, timeout: int = 60000) -> None:
        """Ждём, пока Streamlit закончит выполнение страницы."""
        self.page.wait_for_timeout(600)
        self.page.wait_for_function("() => !document.querySelector('[data-testid=\"stStatusWidget\"]')",
                                    timeout=timeout)
        self.page.wait_for_timeout(700)

    def messages(self) -> int:
        return self.page.locator('[data-testid="stChatMessage"]').count()

    def ask(self, question: str) -> None:
        before = self.messages()
        box = self.page.locator("textarea").first
        box.click()
        box.type(question, delay=35)
        self.page.keyboard.press("Enter")
        self.page.wait_for_function(f"() => document.querySelectorAll('[data-testid=\"stChatMessage\"]').length >= "
                                    f"{before + 2}", timeout=90000)
        self.idle()

    def click(self, locator) -> None:
        before = self.messages()
        locator.click()
        self.idle()
        if self.messages() > before:
            self.page.wait_for_timeout(300)

    def _scroll(self, how: str) -> None:
        self.page.evaluate(f"() => {{ const m = ({SCROLLER_JS})(); {how} }}")
        self.page.wait_for_timeout(900)

    def scroll_bottom(self) -> None:
        self._scroll("m.scrollTo({top: m.scrollHeight, behavior: 'smooth'});")

    def scroll_top(self) -> None:
        self._scroll("m.scrollTo({top: 0});")

    def scroll_by(self, dy: int) -> None:
        self._scroll(f"m.scrollBy({{top: {dy}, behavior: 'smooth'}});")

    def shot(self, name: str) -> None:
        self.page.evaluate(CAPTION_JS, "")
        SCREENS.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(SCREENS / f"{name}.png"))


def run(url: str, pause: float, headless: bool) -> Path:
    VIDEO.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=headless)
        context = browser.new_context(viewport={"width": 1280, "height": 720}, device_scale_factor=1, locale="ru-RU")
        page = context.new_page()
        d = Demo(page, pause)
        frames: list[tuple[float, bytes]] = []
        cdp = context.new_cdp_session(page)

        def on_frame(params):
            frames.append((time.time(), base64.b64decode(params["data"])))
            cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
        cdp.on("Page.screencastFrame", on_frame)
        page.goto(url)
        page.wait_for_selector("textarea", timeout=120000)
        d.idle()
        cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 88, "maxWidth": 1280, "maxHeight": 720})

        d.scroll_top()
        d.caption("«Первый чек» — ИИ-помощник для начинающих самозанятых в приложении банка", 3.5)
        d.shot("01_glavnyy_ekran")
        d.scroll_by(330)
        d.caption("Аня, 19 лет, самозанятая. Банк видит её поступления и сам подсказывает, что требует внимания", 4)
        d.shot("01b_podskazki")

        d.click(page.get_by_role("button", name=re.compile("Подробнее")).first)
        d.scroll_bottom()
        d.caption("Бывший работодатель заплатил ей за дизайн — такой доход не облагается НПД: ч. 2 ст. 6 422-ФЗ", 4.5)
        d.shot("02_byvshiy_rabotodatel")
        d.click(page.locator("button", has_text="422-ФЗ, ст. 6").last)
        page.wait_for_timeout(700)
        d.caption("Каждая ссылка открывает текст закона", 2.5)
        d.shot("03_istochnik")
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)

        d.ask("можно на самозанятости перекупать кроссовки и продавать дороже?")
        d.scroll_bottom()
        d.caption("Вопрос разговорным языком — ответ простыми словами со ссылками на закон", 4)
        d.shot("04_otvet_po_zakonu")

        d.ask("Сколько стоит патент для ИП в Москве?")
        d.scroll_bottom()
        d.caption("Этого в законе нет — честный отказ. Вопрос можно передать специалисту банка", 3.5)
        d.click(page.get_by_role("button", name=re.compile("Передать вопрос специалисту")).last)
        d.scroll_bottom()
        d.shot("05_chestnyy_otkaz")

        page.get_by_text("Чеки и сроки", exact=True).first.click()
        d.idle()
        d.caption("Не только совет, но и действие: «Спасибо!» от Кирилла — это оплата за работу?", 4)
        d.click(page.get_by_role("button", name=re.compile("Оплата за работу")).first)
        d.caption("Черновик чека по реквизитам ч. 6 ст. 14 — сформировать через банк в одно нажатие", 4)
        d.shot("11_cheki")
        d.click(page.get_by_role("button", name=re.compile("Сформировать чек через банк")).first)
        d.scroll_by(900)
        d.caption("Календарь: срок чека, уведомление ФНС до 12-го, налог до 28-го, автоплатёж через банк", 4)
        d.shot("12_sroki")

        page.locator('[data-testid="stSelectbox"]').first.click()
        page.get_by_role("option", name=re.compile("Максим")).click()
        d.idle()
        page.get_by_text("Помощник", exact=True).first.click()
        d.idle()
        d.scroll_top()
        d.scroll_by(330)
        d.caption("Максим: 2,25 млн ₽ с начала года. При нынешнем темпе лимит закончится около 27 октября", 4.5)
        d.shot("06_maksim_u_limita")
        d.scroll_top()
        d.click(page.get_by_role("button", name=re.compile("Следующий шаг")))
        d.scroll_bottom()
        d.caption("Самозанятость или ИП — расчёт на его доходе по НК РФ, и предложение открыть ИП и счёт заранее", 5)
        d.shot("07_ip_ili_samozanyatost")

        page.locator('[data-testid="stSelectbox"]').first.click()
        page.get_by_role("option", name=re.compile("Лиза")).click()
        d.idle()
        page.get_by_text("Подходит ли мне НПД", exact=True).first.click()
        d.idle()
        d.caption("Лиза пока не самозанятая. Проверка «подходит ли мне» — по правилам закона, без нейросети", 3.5)
        d.click(page.get_by_role("button", name=re.compile("Проверить$")))
        d.scroll_by(700)
        d.caption("Вывод со ссылками на пункты 422-ФЗ — и постановка на учёт через банк", 4)
        d.shot("13_podhodit")

        page.get_by_text("Для банка", exact=True).first.click()
        d.idle()
        page.get_by_role("tab", name="Лиды").click()
        page.wait_for_timeout(800)
        d.caption("Для банка — лиды: кому предложить ИП и РКО, чеки и автоплатёж через банк, постановку на учёт", 4.5)
        d.shot("14_lidy")
        page.get_by_role("tab", name="Вопросы специалисту").click()
        page.wait_for_timeout(800)
        d.caption("Ответ специалиста публикуется в базу знаний — дальше помощник отвечает сам", 4)
        d.shot("15_specialist")
        page.get_by_role("tab", name="Экономика").click()
        page.wait_for_timeout(800)
        d.shot("16_ekonomika")
        d.caption("GigaChat 3.1 Lightning внутри контура банка · ответ ~1 с · 61 из 63 ответов по закону верны", 4)
        cdp.send("Page.stopScreencast")
        context.close()

        # мобильный экран — для презентации
        mobile = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, locale="ru-RU",
                                     is_mobile=True, has_touch=True)
        mpage = mobile.new_page()
        mpage.goto(url)
        mpage.wait_for_selector("textarea", timeout=120000)
        mobile_demo = Demo(mpage, pause)
        mobile_demo.idle()
        mobile_demo.scroll_top()
        mpage.screenshot(path=str(SCREENS / "10_telefon.png"), full_page=False)
        mobile.close()
        browser.close()
    return frames


def to_mp4(frames: list[tuple[float, bytes]], target: Path) -> Path:
    """Кадры screencast приходят, только когда страница меняется: каждый держится до следующего."""
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    total = frames[-1][0] - frames[0][0] + 2.0
    speed = max(1.0, total / MAX_SECONDS)       # длиннее двух минут — равномерно ускоряем
    with tempfile.TemporaryDirectory() as tmp:
        lines = []
        for k, (t, data) in enumerate(frames):
            name = Path(tmp) / f"f{k:05d}.jpg"
            name.write_bytes(data)
            duration = ((frames[k + 1][0] - t) if k + 1 < len(frames) else 2.0) / speed
            lines += [f"file '{name.as_posix()}'", f"duration {max(duration, 0.04):.3f}"]
        lines.append(f"file '{(Path(tmp) / f'f{len(frames) - 1:05d}.jpg').as_posix()}'")
        concat = Path(tmp) / "frames.txt"
        concat.write_text("\n".join(lines), encoding="utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat),
                        "-vf", "fps=25,scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2:white",
                        "-c:v", "libx264", "-preset", "slow", "-crf", "24", "-pix_fmt", "yuv420p",
                        "-movflags", "+faststart", str(target)], check=True)
    return target


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8501")
    ap.add_argument("--pause", type=float, default=4.0, help="сколько секунд держать подпись")
    ap.add_argument("--show", action="store_true", help="показывать окно браузера")
    args = ap.parse_args()
    frames = run(args.url, args.pause, headless=not args.show)
    out = to_mp4(frames, VIDEO / "pervyy_chek_demo.mp4")
    print(f"Видео: {out}\nСкриншоты: {SCREENS}")


if __name__ == "__main__":
    sys.exit(main())
