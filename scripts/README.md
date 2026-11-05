# Локальный журнал кампании

`job_ledger.py` использует только Python 3 stdlib и SQLite, не содержит сетевых адаптеров и ничего не отправляет.

Поле approval, доказательство идентичности вакансии, receipt и external evidence являются записанными декларациями: скрипт не устанавливает их истинность, авторство, полномочия или соответствие содержательным критериям кампании.

Успешная reservation означает только сохранённую локальную блокировку повторной попытки; разрешение внешнего действия проверяется отдельно по актуальному поручению пользователя и правилам канала.

Храните БД, JSON-входы и артефакты в приватном каталоге вне публичного репозитория с ограниченными правами доступа.

БД содержит адресатов, пути и ссылки на доказательства; содержимое артефактов и исходные тексты вакансий в БД не копируются.

Команды возвращают только ID, состояния, SHA-256 и очередь; ошибки не выводят входные данные.

## Вызовы

```sh
python3 scripts/job_ledger.py --db /private/campaign/jobs.sqlite import --input /private/campaign/vacancies.json
python3 scripts/job_ledger.py --db /private/campaign/jobs.sqlite policy --input /private/campaign/permission.json
python3 scripts/job_ledger.py --db /private/campaign/jobs.sqlite prepare --input /private/campaign/prepare.json
python3 scripts/job_ledger.py --db /private/campaign/jobs.sqlite review --input /private/campaign/review.json
python3 scripts/job_ledger.py --db /private/campaign/jobs.sqlite reserve --input /private/campaign/reserve.json
python3 scripts/job_ledger.py --db /private/campaign/jobs.sqlite outcome --input /private/campaign/outcome.json
python3 scripts/job_ledger.py --db /private/campaign/jobs.sqlite block --input /private/campaign/block.json
python3 scripts/job_ledger.py --db /private/campaign/jobs.sqlite block-company --input /private/campaign/company-block.json
python3 scripts/job_ledger.py --db /private/campaign/jobs.sqlite revoke-policy --input /private/campaign/revoke.json
python3 scripts/job_ledger.py --db /private/campaign/jobs.sqlite queue
python3 scripts/job_ledger.py --db /private/campaign/jobs.sqlite status
python3 -m unittest discover -s tests -v
```

Успех имеет код выхода 0, ошибка валидации или хранения — 2.

Все входы — JSON UTF-8 с указанными полями, без дополнительных полей; повторяющиеся ключи, NaN и Infinity запрещены.

ID — непустые строки, SHA-256 — 64 строчных шестнадцатеричных символа, каналы — `hh`, `ats`, `email`.

Обозначения `<SHA256 ...>` ниже нужно заменить фактическими хешами, а даты — актуальным сроком разрешения.

## Схемы входов

`import` принимает следующий объект:

```json
{"vacancies":[{"source":"hh","id":"synthetic-123","opportunity_id":"company-A:REQ-1","company_id":"company-A","identity_evidence":"Employer requisition REQ-1; exact source reference","content":{"title":"Synthetic role","description":"Synthetic vacancy snapshot"}}]}
```

Пара `(source,id)` устойчива и не может быть переназначена другой opportunity; хранится как два поля, без неоднозначного склеивания строк.

Одинаковый `opportunity_id` объединяет копии из разных каналов только после внешней проверки доказанной идентичности; совпадение названия не является основанием.

Скрипт требует явные `opportunity_id`, `company_id` и `identity_evidence`, но сам не доказывает соответствие этих заявлений реальности.

`content` — непустой JSON-объект точного локального снимка вакансии; его канонический SHA-256 и сведения источников образуют версию opportunity.

Повторный импорт неизменных данных не создаёт записей и возвращает пустой `changed`.

Изменение вакансии, источников или identity evidence отзывает ревью, включая случай возврата к прежнему тексту.

`policy` записывает однажды задекларированное разрешение:

```json
{"id":"permission-v1","expires_at":"2030-01-02T00:00:00Z","allowed_channels":["hh","ats","email"],"max_total":10,"max_daily":3,"policy_version":"1","opportunity_ids":["company-A:REQ-1"],"company_ids":[],"approval_digest":"<SHA256 of exact permission record>"}
```

Срок строго в UTC `YYYY-MM-DDTHH:MM:SSZ`, без дробных секунд и смещения; разрешение истекает в указанный момент.

Лимиты — положительные целые числа, JSON boolean вместо числа недопустим.

Scope задаётся непустым объединением списков `opportunity_ids` и `company_ids`; совпадения одного списка достаточно.

Назначайте company scope только если действующее разрешение действительно охватывает его; содержательные критерии разрешения, возможность расширения очереди и отзыв разрешения проверяет ведущий вне CLI.

Policy ID неизменяем: повтор той же записи допустим, изменения требуют нового ID и нового хеша.

Ответ содержит `policy_id` и `policy_hash`, которые передаются в reserve.

Лимиты считаются по всей БД, независимо от ID/версии политики, и учитывают `reserved`, `unknown`, `sent`, `rejected`, `no_contact`; смена политики и UTC даты не обнуляет общий расход.

Дневной расход определяется UTC датой reservation, переход в sent не создаёт второй расход.

Только доказанный `not_sent` освобождает расход; для независимой кампании используйте отдельную БД, но не создавайте новую БД для обхода лимитов, неизвестного результата или отказа.

`prepare` фиксирует канал, адресата, версию вакансии и SHA-256 фактических байтов одного готового файла:

```json
{"opportunity_id":"company-A:REQ-1","channel":"hh","recipient":"synthetic listing synthetic-123","artifact_path":"/private/campaign/prepared-payload.json"}
```

Если канал требует несколько материалов, подготовьте неизменяемый локальный пакет, включающий точное содержимое всех предназначенных к передаче полей и файлов, и хешируйте его как артефакт.

Скрипт не знает семантику такого пакета и не проверяет ссылки внутри него; проверка соответствия реальной отправки пакету остаётся у исполнителя.

Ответ содержит `payload_digest` и `artifact_hash`; payload digest связывает ID, версию вакансии, канал, адресата, абсолютный путь и SHA-256 артефакта.

Изменение любого поля подготовки отзывает ревью; изменение фактических байтов файла выявляется при review, reserve и чтении очереди.

Перезапись подготовки после `reserved`, `unknown`, `sent` или блокировки запрещена.

`review` записывает декларацию актуального одобрения:

```json
{"opportunity_id":"company-A:REQ-1","payload_digest":"<SHA256 from prepare>","approval_digest":"<SHA256 of exact current review record>"}
```

Скрипт проверяет совпадение версий и байтов, но не подменяет независимую содержательную проверку по workflow.

`reserve` атомарно проверяет блокировки, текущий review, байты артефакта, policy hash, срок, канал, scope и лимиты:

```json
{"opportunity_id":"company-A:REQ-1","policy_id":"permission-v1","policy_hash":"<SHA256 from policy>","payload_digest":"<SHA256 from prepare>"}
```

Успех возвращает `reservation_id`, `opportunity_id` и `status: reserved`; reservation сохраняет снимок подготовки, approval digest и policy hash.

Сделайте reservation до внешней попытки; сбой процесса после неё не является доказательством отсутствия отправки и не позволяет повторить попытку.

`outcome` принимает ровно один из исходов:

```json
{"reservation_id":"<ID from reserve>","status":"sent","receipt":"exact external receipt reference"}
```

```json
{"reservation_id":"<ID from reserve>","status":"unknown"}
```

```json
{"reservation_id":"<ID from reserve>","status":"not_sent","external_evidence":"exact external evidence confirming no submission"}
```

```json
{"reservation_id":"<ID from reserve>","status":"rejected","external_evidence":"exact explicit refusal reference"}
```

```json
{"reservation_id":"<ID from reserve>","status":"no_contact","external_evidence":"exact prohibition reference"}
```

`sent` требует непустой receipt, `not_sent`, `rejected` и `no_contact` требуют непустое external evidence; оба поля допускаются у любого исхода.

Из `reserved` и `unknown` можно записать указанные исходы; из `sent` можно перейти только в `rejected` или `no_contact`, сохранив receipt.

`not_sent` окончателен для данной попытки, очищает подготовку и review и позволяет только новую подготовку с новым review.

`reserved` и `unknown` блокируют повтор независимо от времени; автоматического TTL и автоматического retry нет.

`rejected` и `no_contact` окончательно блокируют все каналы той же opportunity; импорт новой версии блокировку не снимает.

Для отказа, известного ещё до reservation, служит `block`:

```json
{"opportunity_id":"company-A:REQ-1","reason":"no_contact","external_evidence":"exact explicit prohibition reference"}
```

`reason` может быть `rejected` или `no_contact`; обратной команды разблокировки нет.

Если запрет явно относится ко всей компании, используйте `block-company`:

```json
{"company_id":"company-A","external_evidence":"exact company-wide no-contact reference"}
```

Он блокирует prepare, review и reserve для всех текущих и будущих opportunity с данным company ID, независимо от канала и policy scope; команда допустима до первого импорта вакансий этой компании.

Блокировка не удаляет reservation и её фактический исход; обратной команды нет, повтор сохраняет первоначальное доказательство.

`revoke-policy` сохраняет явный отзыв существующей политики:

```json
{"policy_id":"permission-v1","external_evidence":"exact withdrawal reference"}
```

Отозванный ID больше нельзя использовать в reserve, в том числе после повторного импорта policy; повтор отзыва сохраняет первоначальное основание.

Отзыв не отменяет уже совершённые действия, не снимает reservation и не даёт права повторить попытку; исход ранее зарезервированной попытки фиксируется по внешним данным.

Новую политику после отзыва можно записывать только в пределах нового действующего поручения; CLI не устанавливает его наличие.

`queue` выводит только ID и версии новых или изменённых opportunity, которым требуется подготовка/актуальное ревью, исключая заблокированные и уже предпринятые попытки.

Opportunity с актуальным review исчезает из очереди подготовки, но остаётся доступной для reserve по известному ID.

`status` без input возвращает локальную картину восстановления, включая скрытые из очереди reservation:

```json
{"opportunities":[{"opportunity_id":"company-A:REQ-1","company_id":"company-A","state":"reserved","version":"<SHA256 vacancy version>","payload_digest":null}],"attempts":[{"reservation_id":"<ID>","opportunity_id":"company-A:REQ-1","state":"reserved","reserved_at":"2030-01-01T00:00:00Z","payload_digest":"<SHA256 prepared payload>","policy_id":"permission-v1","policy_hash":"<SHA256 policy>"}],"revoked_policies":[],"blocked_companies":[]}
```

Состояния opportunity: `needs_preparation`, `needs_review`, `ready`, `reserved`, `unknown`, `sent`, `blocked`; блокировка имеет приоритет над состоянием попытки, которое всегда видно отдельно в `attempts`.

`ready` здесь означает только наличие текущей записи review для фактических байтов артефакта, а не содержательную готовность по workflow и не разрешение отправки.

Для `needs_preparation` digest равен null; при активной попытке или блокировке digest текущей подготовки не проверяется и также равен null, а исходный digest доступен в записи попытки.

`attempts` содержит все попытки, включая `not_sent`, с исходным временем reservation UTC и хешами для восстановления; receipt, адресат, пути, текст и evidence в status не выводятся.

`revoked_policies` содержит `policy_id` и `revoked_at`, `blocked_companies` — `company_id` и `blocked_at`; обе отметки времени имеют формат UTC.

## Хранение и границы

Таблицы `opportunities`, `sources`, `policies`, `attempts` и `blocks` хранят соответственно текущие версии, устойчивые источники, неизменяемые политики, попытки и основания блокировки.

Таблицы `company_blocks` и `policy_revocations` сохраняют запреты по company ID и отзывы policy с UTC временем и внешним основанием; новые таблицы добавляются также при открытии ранее созданной БД.

Все операции выполняются в SQLite `BEGIN IMMEDIATE`; один writer обрабатывает команду целиком или откатывает её при ошибке, параллельный writer не может зарезервировать ту же opportunity повторно.

SQLite журнал защищает последовательность локальных записей, но не является неизменяемым аудитом: владелец файлов может изменить БД, а инструмент не предотвращает внешнюю отправку в обход журнала.

Код не реализует API интеграцию, поиск вакансий, ATS score, браузер, email transport, проверку секрета, отправку и производственную гарантию доставки.

Между проверкой хеша и внешней отправкой файл может быть изменён другим процессом; исполнитель обязан сверить передаваемые байты с сохранённым digest и не менять пакет после reservation.

Фиксируйте отзыв через `revoke-policy`; актуальность поручения проверяется перед внешним действием независимо от локального срока и сохранённых деклараций.
