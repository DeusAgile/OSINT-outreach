# Шаблоны поисковых запросов

Читается `scripts/media_search.py` по имени набора. Каждый набор — блок кода
со строками-шаблонами; `{company}`, `{person}`, `{year}` подставляются из
аргументов скрипта. Шаблон, которому не хватает параметра (например
`{person}` в наборе `person_mentions` без `--person`), молча пропускается —
это не ошибка.

Расширяется правкой этого файла, без изменения `media_search.py`.

**Набор `company_leadership`:**
```
{company} руководство команда
{company} основатель CEO
site:setka.ru {company}
{company} сетка hh.ru
site:career.habr.com {company} сотрудники
{company} топ-менеджмент
```

**Набор `hr_process`:**
```
site:dreamjob.ru {company}
{company} отзывы сотрудников собеседование
{company} как проходит собеседование
{company} этапы отбора кандидатов
site:habr.com {company} как мы нанимаем
site:vk.com {company} вакансии команда
site:comnews.ru {company} кадры персонал
site:amlive.ru {company}
site:tenchat.ru {company} найм HR
```

**Набор `lpr_search`:**
```
"HRD" {company}
"директор по персоналу" {company}
"руководитель отдела подбора" {company}
"Head of Recruitment" {company}
"HR business partner" {company}
"HR-тимлид" {company}
"HR Team Lead" {company}
"HR-директор" {company}
"руководитель HR" {company}
{company} HR интервью
site:career.habr.com {company}
site:tenchat.ru {company} HR
```

**Набор `person_mentions`:**
```
"{person}" {company}
"{person}" HR
"{person}" интервью
"{person}" site:t.me
"{person}" site:setka.ru
"{person}" site:vc.ru OR site:rb.ru OR site:cnews.ru OR site:comnews.ru
"{person}" конференция доклад
```

**Набор `company_insights`:**
```
{company} исследование HR {year}
{company} найм автоматизация
{company} новости {year}
```
