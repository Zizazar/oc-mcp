# OpenComputers MCP для Codex

Мост для **Minecraft 1.7.10** и OpenOS. Lua-агент проверен с runtime и библиотекой ввода-вывода из OpenComputers 1.8.10. Один MCP-сервер обслуживает все игровые компьютеры в локальном мире. На каждом нужен диск с установленной OpenOS, Internet Card и одна команда установки.

## Свой сервер в Docker с Traefik

GitHub Actions проверяет и публикует образ `ghcr.io/zizazar/oc-mcp:latest` для Linux x86-64 после каждого обновления `main`. Сервер скачивает готовый образ. Ключ моста хранится в томе `oc-mcp_state`, поэтому адреса подключения сохраняются при обновлениях и перезапусках.

На VPS достаточно одного `compose.yaml`. Направьте DNS-запись поддомена на сервер с Traefik. Traefik должен использовать Docker provider и быть подключён к сети, указанной в `TRAEFIK_NETWORK`. [Traefik Docker provider](https://doc.traefik.io/traefik/providers/docker/)

```bash
curl -fsSLo compose.yaml https://raw.githubusercontent.com/Zizazar/oc-mcp/main/compose.yaml
export OC_MCP_DOMAIN=oc.example.com TRAEFIK_NETWORK=traefik
export TRAEFIK_HTTPS_ENTRYPOINT=websecure TRAEFIK_CERTRESOLVER=letsencrypt
docker compose --profile https up -d --pull always --wait
docker compose --profile https exec -T relay python relay.py install server-main
```

Подставьте имя своего HTTPS entrypoint и resolver для сертификатов. Если Traefik уже подбирает сертификат по домену, оставьте `TRAEFIK_CERTRESOLVER` пустым. Переменные можно сохранить в `.env` вместо `export`. Выполните выведенную команду в терминале игрового компьютера с OpenOS и Internet Card. Для каждого компьютера задайте своё имя.

Codex подключается напрямую к `https://oc.example.com/mcp` с Bearer-токеном. На VPS получите токен для своего сервера. При обновлениях токен остаётся тем же, пока используется том `oc-mcp_state`.

```bash
docker compose --profile https exec -T relay python relay.py mcp-token
```

На компьютере с Codex подставьте свой домен, введите токен в PowerShell 7 и зарегистрируйте сервер. Перезапустите приложение Codex после настройки.

```powershell
$env:OC_MCP_TOKEN = Read-Host 'MCP token' -MaskInput
[Environment]::SetEnvironmentVariable('OC_MCP_TOKEN', $env:OC_MCP_TOKEN, 'User')
codex mcp add opencomputers --url https://oc.example.com/mcp --bearer-token-env-var OC_MCP_TOKEN
```

Для обновления с теми же переменными запустите на VPS `docker compose --profile https up -d --pull always --wait`. Если в репозитории изменился `compose.yaml`, скачайте его заново. Сохраняйте том `oc-mcp_state` при резервном копировании и удалении контейнеров.

Для локального Docker без HTTPS запустите `docker compose up -d --pull always --wait` из той же папки. Адрес для Codex выдаёт `docker compose exec -T bridge python bridge.py mcp-url`, а команду установки для локального мира выдаёт `docker compose exec -T bridge python bridge.py install main`.

## Запуск на Windows

Из папки проекта установите зависимости и запустите мост. Подходит Python 3.11 или новее.

```powershell
python -m pip install -r requirements.txt
python bridge.py serve
```

Оставьте мост запущенным, пока работаете с компьютерами. В другом окне PowerShell один раз зарегистрируйте его в Codex. После регистрации переоткройте Codex, если инструменты ещё не появились.

```powershell
codex mcp add opencomputers --url (python bridge.py mcp-url)
```

По умолчанию мост использует `127.0.0.1:8765`. Если порт занят, укажите один и тот же `--port` перед `serve`, `mcp-url` и `install`. В файле `.oc-mcp-key` хранятся данные для образования адресов подключения. Сохраните его между запусками моста и держите его и сгенерированные адреса у себя.

## Подключение игровых компьютеров

В сборках, где OpenComputers блокирует обращения к локальным адресам, добавьте правило для `127.0.0.1` в `config/OpenComputers.cfg` **вашего экземпляра Minecraft**. В `opencomputers.internet.filteringRules` поставьте `"allow ip:127.0.0.1"` перед `"deny private"` и `"deny bogon"`, оставив остальные правила на месте. Проверьте, что `enableHttp` равен `true`, и перезапустите Minecraft после изменения. Это разрешение на адрес целиком для Internet Card, включая другие его порты. Используйте его в своём локальном мире. В старых сборках с другим форматом конфига проверьте встроенный `application.conf` из JAR мода. [Конфигурация OpenComputers для 1.7.10](https://github.com/MightyPirates/OpenComputers/blob/master-MC1.7.10/src/main/resources/application.conf)

Сгенерируйте команду установки для первого компьютера, например `main`.

```powershell
python bridge.py install main
```

Вставьте полученную строку целиком в терминал **игрового компьютера**. Она скачает один Lua-скрипт, установит его как службу OpenOS и запустит. Служба стартует после перезагрузки компьютера и сама переподключается к мосту. Для второго компьютера сгенерируйте строку с другим именем, например `python bridge.py install reactor`. [Службы OpenOS](https://ocdoc.cil.li/api:rc)

## Подключение с Minecraft-сервера

Cloudflare Quick Tunnel даёт временный HTTPS-адрес без VPS, домена и аккаунта. `bridge.py serve` и relay работают на вашем Windows-ПК, пока вы используете Codex. На Minecraft-сервере для игрового компьютера по-прежнему нужны OpenOS и Internet Card. Сервер должен разрешать карте HTTPS-запросы к адресу туннеля. [Cloudflare Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)

Установите `cloudflared` и добавьте его в `PATH`, если команды ещё нет. При работающем мосте запустите relay в отдельном окне PowerShell. [Загрузки Cloudflare](https://developers.cloudflare.com/tunnel/downloads/)

```powershell
python relay.py serve
```

В другом окне сгенерируйте установку для игрового компьютера на сервере и вставьте выведенную строку в его терминал.

```powershell
python relay.py install server-main
```

Локальные и серверные компьютеры появляются в одном `list_computers`. Публичный адрес ведёт к установщику и API игрового агента. Оба требуют ключ своего компьютера. У MCP остаётся локальный адрес, уже указанный в Codex. Держите команды установки у себя, поскольку они содержат ключи. Трафик серверных ПК проходит через Cloudflare.

После перезапуска Quick Tunnel сгенерируйте и выполните команду установки ещё раз для каждого серверного ПК. Имя можно оставить тем же. Если нужен постоянный адрес, направьте свой HTTPS-туннель на `127.0.0.1:8766` и запустите relay с его доменом. Для другого порта моста передайте `--bridge-port` перед `serve`.

```powershell
python relay.py serve --public-url https://relay.example.com
```

### Постоянный адрес через Tailscale Funnel

Установите Tailscale и войдите в аккаунт на Windows-ПК с мостом. На стороне Minecraft достаточно Internet Card с доступом к публичному HTTPS. Funnel выдаёт постоянный адрес `*.ts.net` и расшифровывает HTTPS на вашем ПК. Промежуточные серверы Funnel передают зашифрованный трафик. [Документация Tailscale Funnel](https://tailscale.com/docs/features/tailscale-funnel#how-funnel-works)

Откройте отдельное окно PowerShell и запустите Funnel на порт relay `8766`. При первом запуске Tailscale может открыть страницу, где нужно включить Funnel.

```powershell
tailscale funnel 8766
```

Скопируйте HTTPS-адрес из вывода. В другом окне запустите relay с этим адресом, затем сгенерируйте команду `python relay.py install server-main` в ещё одном окне. Когда адрес остаётся прежним, установленный игровой агент сам переподключается после перезапуска моста или Funnel.

```powershell
python relay.py serve --public-url https://my-pc.my-tailnet.ts.net
```

### Временный адрес через localhost.run

При запущенном `bridge.py serve` откройте PowerShell и запустите SSH-туннель к порту relay. В бесплатном режиме `nokey` аккаунт и SSH-ключ не нужны. [Документация localhost.run](https://localhost.run/docs/faq#i-don-t-want-to-use-an-ssh-key)

```powershell
ssh -o ServerAliveInterval=60 -R 80:127.0.0.1:8766 nokey@localhost.run
```

Оставьте SSH запущенным. В другом окне PowerShell подставьте HTTPS-адрес из его вывода, а команду установки сгенерируйте через `python relay.py install server-main`.

```powershell
python relay.py serve --public-url https://example.lhr.life
```

После смены бесплатного адреса перезапустите relay с новым URL и повторите установку на игровом компьютере. HTTPS расшифровывается на стороне localhost.run, через который проходят запросы агента. [HTTP-туннели localhost.run](https://localhost.run/docs/http-tunnels)

## Работа из Codex

Можно написать «Покажи компьютеры OpenComputers. Используй `reactor` и прочитай экран». В каждом вызове Codex передаёт `target`, то есть имя или полный ID из `list_computers`. Если два компьютера получили одинаковое имя, выберите один по ID. У каждого компьютера своя очередь из одного вызова. С другими компьютерами можно работать одновременно.

| Инструмент | Действие |
| --- | --- |
| `list_computers` | Имена, стабильные адреса компьютеров и статус связи |
| `run_command` | Команда OpenOS с рабочей папкой, stdin, stdout, stderr и кодом выхода |
| `list_files` | Список файлов и каталогов |
| `read_file`, `write_file` | Чтение и запись текста или двоичных данных через Base64 |
| `read_screen` | Строки символов экрана основного GPU |
| `send_input` | Вставка текста и нажатия клавиш в открытой программе |

`run_command` запускается в отдельном процессе OpenOS. Для программы, работающей в видимом терминале, используйте `read_screen` и `send_input`. Инструмент экрана возвращает символы без цветов. Временно приостановленный мир или выгруженный чанк приостанавливает и агента.

На игровом компьютере выполните `rc oc-mcp status`, чтобы проверить состояние. Для остановки используйте `rc oc-mcp stop`, а для выключения автозапуска `rc oc-mcp disable`. Повторная установка с другим именем заменит имя компьютера.
