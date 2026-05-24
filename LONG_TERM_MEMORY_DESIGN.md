# HandQ 长期记忆系统 —— 设计文档（索引）

> **状态**：v2 设计稿（已用于实现）
> **目标实现版本**：HandQ 3.1+
> **关联文档**：[ARCHITECTURE.md](./ARCHITECTURE.md)
> **设计依据**：逆向 Yansu 0.1.296 windows-x64 桌面端二进制，配合 HandQ 现有代码盘点。

---

## 关键改动 vs v1

v1 只看到 yansu 的 memory 一条线，v2 把整个设计还原后**发现是 memory + knowledge 双轨制**，并且配套有：

- 5 维 activity triage（一次 LLM 同时给出 worth_memory / worth_knowledge / worth_crystal / worth_handoff / worth_automation 五个判定）
- L1/L2/L3 三层级 dream synthesis（短期 → 模式 → 元洞察）
- 4 个 crystallizer 协同（memory / knowledge / suggestion / activity）
- chunk-based 存储（一个 entry 切多 chunk，FTS 索引在 chunk 级）
- [SELF] / [OTHER] 标注系统（区分用户自述 vs 第三方陈述）
- PII 检测层（ML 模型 + regex 双层）
- 知识文件系统镜像 `.something/knowledge/*.md`（项目本地）

v2 的 HandQ 设计也调整为**双轨制**起步，避免后续再迁移。

---

## 文档地图

完整设计被拆到 `docs/long_term_memory/` 子目录下，按模块独立可读：

| 文件 | 目的 | 实现时优先级 |
|---|---|---|
| [01_yansu_reference.md](./docs/long_term_memory/01_yansu_reference.md) | **Yansu 实际设计完整还原** —— 二进制逆向出来的全部细节，是其余文档的"权威来源"。读这一份能完整理解 yansu 怎么做记忆 | 必读 |
| [02_handq_design.md](./docs/long_term_memory/02_handq_design.md) | HandQ 的适配方案：抄什么、简化什么、为什么 | 必读 |
| [03_schema.md](./docs/long_term_memory/03_schema.md) | SQLite 完整 DDL（含 memory、knowledge、双 FTS、embedding 缓存、迁移） | 实现时 |
| [04_modules.md](./docs/long_term_memory/04_modules.md) | Python 包/类划分，每个文件的职责 | 实现时 |
| [05_triage_prompts.md](./docs/long_term_memory/05_triage_prompts.md) | 完整 triage prompts（memory + knowledge 联合判定，可直接粘贴） | 实现时 |
| [06_integration.md](./docs/long_term_memory/06_integration.md) | 改哪几个现有文件，每处怎么改（含 file:line） | 实现时 |
| [07_phasing.md](./docs/long_term_memory/07_phasing.md) | P0~P6 分阶段交付计划与验收 | 项目管理 |
| [08_skeletons.md](./docs/long_term_memory/08_skeletons.md) | 关键文件的可运行代码骨架 | 实现时 |
| [09_yansu_evidence.md](./docs/long_term_memory/09_yansu_evidence.md) | 逆向出来的 yansu 字符串证据归档（核对用） | 验证 / 复盘 |

---

## 三个被锁死的关键决策

| # | 决策 | 选择 | 详见 |
|---|---|---|---|
| **D1** | 持久化媒介 | **SQLite（长期知识）+ 文件系统（session、artifacts）混合** | [02_handq_design.md §3](./docs/long_term_memory/02_handq_design.md) |
| **D2** | 记忆维度 | **memory + knowledge 双轨制（v2 升级）** | [01_yansu_reference.md §2](./docs/long_term_memory/01_yansu_reference.md) / [02_handq_design.md §4](./docs/long_term_memory/02_handq_design.md) |
| **D3** | GEP 改造 | **保留 GEP 概念，复用本系统基础设施，作为第三轨"procedure"加入** | [02_handq_design.md §10](./docs/long_term_memory/02_handq_design.md) |

---

## 下一步

实现入口：[07_phasing.md](./docs/long_term_memory/07_phasing.md) **P0**——按 §03 schema 建库、§04 模块骨架、§06 集成点（仅注入空块占位）跑通 → 一周内可上线。

**EOF**
