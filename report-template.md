# {{company_name}} — отчёт TalentMind Outreach Intelligence

*Сформировано: {{generated_at}} · Источники: {{sources_count}}*

{{#if resolved_automatically}}
> ⚠️ Определено автоматически, требует проверки: {{resolved_automatically}}
{{/if}}
{{#if needs_review}}
> 🔶 **Неоднозначность при определении компании — выбран наиболее вероятный
> вариант автоматически, отчёт требует проверки перед использованием.**
> Выбрано: {{chosen_candidate}}. Альтернатива с равным весом:
> {{alternate_candidate}}. Причина, по которой не удалось разрешить
> однозначно: {{needs_review_reason}}.
{{/if}}

## 1. Сфера деятельности

{{industry_summary}}

- Отрасль: {{industries}}
- Регион: {{region}}
- Сайт: {{site}}{{#if site_alternates}} (+ {{site_alternates}} — тот же
  субъект, определено по пересечению домена/контактов, см. п.4c
  `modules/01-company-resolve.md`){{/if}}
- hh.ru: {{hh_url}}
{{#if accredited_it}}- Аккредитована как ИТ-компания{{/if}}

{{#each company_facts}}
- {{this.text}} — [источник]({{this.url}})
{{/each}}

## 2. Ключевые инсайты за {{insights_period_months}} мес.

{{#each insights}}
{{@index}}. {{this.text}} — [источник]({{this.url}})
{{else}}
_Инсайты за период не найдены в открытых источниках._
{{/each}}

## 3. ФИО и контактные данные ЛПР/ЛВР

{{#each persons}}
### {{this.name_normalized}} {{#if this.position}}— {{this.position}}{{/if}}

- Confidence: **{{this.confidence_suggested}}**
- Привязка к компании: {{this.company_link.type}} ([источник]({{this.company_link.evidence_url}}))
- Другие варианты написания имени: {{this.name_variants}}
{{else}}
_ЛПР не найден в открытых источниках._
{{/each}}

## 4. Публичные страницы контактных лиц

{{#each persons}}
**{{this.name_normalized}}:**
{{#each this.public_profiles}}
- [{{this.label}}]({{this.url}})
{{else}}
- не найдено
{{/each}}
{{/each}}

## 5. Описание интересов (цифровой след)

{{#each persons}}
**{{this.name_normalized}}:**
{{#each this.digital_footprint}}
- {{this.summary}} — [источник]({{this.url}})
{{else}}
- цифровой след не найден в открытых источниках
{{/each}}
{{/each}}

## 6. Персонализированные материалы

См. отдельные файлы черновиков (`telegram-draft-template.md`,
`email-draft-template.md`) — генерируются модулем `06-outreach-drafts`
исключительно на основе фактов из блоков 1–5 выше.

---

*Источники с ограниченным охватом (partial): {{partial_sources}}*
*Автоматически определённые значения требуют проверки перед использованием
во внешней коммуникации.*
