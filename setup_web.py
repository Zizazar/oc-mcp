"""Public self-service setup for OpenComputers and MCP."""

from collections.abc import Callable
from html import escape
from pathlib import Path
from secrets import token_urlsafe
from string import Template
from urllib.parse import parse_qs

from starlette.requests import Request
from starlette.responses import HTMLResponse

from credentials import NAME, agent_path, installer_command, issue_account_token, verify_account_token


PAGE = Template(Path(__file__).with_name("setup.html").read_text(encoding="utf-8"))
FORM_LIMIT = 2048


def copy_block(id: str, value: str) -> str:
    return f'''<div class="command">
  <pre><code id="{id}">{escape(value)}</code></pre>
  <button type="button" data-copy="{id}" hidden>Копировать</button>
</div>'''


class SetupPage:
    def __init__(self, key: bytes, public_url: Callable[[], str | None], *, expose_mcp: bool = True):
        self.key = key
        self.public_url = public_url
        self.expose_mcp = expose_mcp

    async def handle(self, request: Request) -> HTMLResponse:
        url = self.public_url()
        if not url:
            return self.render("", "<p>Релей подключается. Обновите страницу через несколько секунд.</p>", 503)
        if not self.expose_mcp:
            return self.render(url, "<p>Для настройки через эту страницу администратору нужно запустить релей с <code>--expose-mcp</code>.</p>")
        if request.method != "POST":
            return self.instructions(url)

        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > FORM_LIMIT:
                return self.instructions(url, error="Данные формы слишком длинные.", status=413)
            body.extend(chunk)
        try:
            form = parse_qs(body.decode("utf-8"), max_num_fields=3)
        except ValueError:
            return self.instructions(url, error="Не удалось прочитать данные формы.", status=400)

        action = form.get("action", [""])[0]
        if action == "register":
            return self.instructions(url, credential=issue_account_token(self.key), issued=True)
        if action != "install":
            return self.instructions(url, error="Выберите действие на странице.", status=400)
        credential = form.get("token", [""])[0].strip()
        name = form.get("name", [""])[0].strip()
        account = verify_account_token(self.key, credential)
        if not account:
            return self.instructions(url, error="Вставьте действующий личный MCP-токен.", status=401, name=name)
        if not NAME.fullmatch(name):
            return self.instructions(url, credential=credential, name=name, status=400,
                                     error="Имя должно начинаться с a–z. Используйте до 24 символов: a–z, 0–9, _ и -.")
        command = installer_command(url + agent_path(self.key, url, "install", name, account))
        return self.instructions(url, credential=credential, name=name, command=command)

    def instructions(self, url: str, *, credential: str = "", issued: bool = False, name: str = "",
                     command: str = "", error: str = "", status: int = 200) -> HTMLResponse:
        mcp_url = f"{url}/mcp"
        codex = f"codex mcp add opencomputers --url {mcp_url} --bearer-token-env-var OC_MCP_TOKEN"
        powershell = (
            "$env:OC_MCP_TOKEN = Read-Host 'Личный MCP-токен' -MaskInput\n"
            "[Environment]::SetEnvironmentVariable('OC_MCP_TOKEN', $env:OC_MCP_TOKEN, 'User')\n"
            + codex
        )
        bash = "read -rsp 'Личный MCP-токен: ' OC_MCP_TOKEN\nprintf '\\n'\nexport OC_MCP_TOKEN\n" + codex
        token_result = ""
        if issued:
            token_result = (
                "<p>Сохраните токен. Он понадобится для настройки MCP и добавления компьютеров.</p>"
                + copy_block("personal-token", credential)
            )
        install_result = copy_block("openos-install", command) if command else ""
        error_message = f'<p class="error" role="alert">{escape(error)}</p>' if error else ""
        content = f'''{error_message}
<section aria-labelledby="register">
  <h2 id="register">1. Получите личный токен</h2>
  <p>Он даёт доступ к игровым компьютерам, подключённым с вашим токеном. Для добавления ещё одного ПК используйте уже выданный токен.</p>
  <form method="post" action="/setup">
    <input type="hidden" name="action" value="register">
    <button class="primary" type="submit">Создать токен</button>
  </form>
  {token_result}
</section>
<section aria-labelledby="openos">
  <h2 id="openos">2. Установите агент в OpenOS</h2>
  <p>На игровом ПК нужны OpenOS и Internet Card. Minecraft-сервер должен разрешать карте HTTPS-запросы к этому релею.</p>
  <form method="post" action="/setup" class="setup-form" autocomplete="off">
    <input type="hidden" name="action" value="install">
    <label>Личный MCP-токен
      <input type="password" name="token" value="{escape(credential)}" maxlength="101" required>
    </label>
    <label>Имя компьютера
      <input name="name" value="{escape(name)}" placeholder="server-main" pattern="[a-z][a-z0-9_\\-]{{0,23}}" maxlength="24" required>
    </label>
    <button class="primary" type="submit">Получить команду</button>
  </form>
  <p>Выполните команду установки в терминале игрового ПК. Служба запустится сразу и будет стартовать при загрузке OpenOS.</p>
  {install_result}
  <p>Проверьте подключение в терминале OpenOS.</p>
  {copy_block("openos-status", "rc oc-mcp status")}
</section>
<section aria-labelledby="mcp">
  <h2 id="mcp">3. Подключите MCP</h2>
  <p>Выполните команды на компьютере с Codex CLI и введите личный токен по запросу.</p>
  <h3>Windows · PowerShell 7</h3>
  {copy_block("powershell", powershell)}
  <p>Перезапустите Codex после настройки.</p>
  <details>
    <summary>macOS и Linux · Bash</summary>
    {copy_block("bash", bash)}
    <p>Запустите Codex из того же терминала.</p>
  </details>
  <details>
    <summary>Другой MCP-клиент</summary>
    <p>Выберите Streamable HTTP и передайте личный токен в заголовке <code>Authorization: Bearer …</code>.</p>
    {copy_block("mcp-url", mcp_url)}
  </details>
  <p>Вызовите <code>list_computers</code>, чтобы увидеть подключённые ПК.</p>
</section>'''
        return self.render(url, content, status)

    @staticmethod
    def render(url: str, content: str, status: int = 200) -> HTMLResponse:
        nonce = token_urlsafe(16)
        return HTMLResponse(
            PAGE.substitute(nonce=nonce, origin=escape(url), content=content), status_code=status,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )
