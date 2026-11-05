# 验证驱动交付 · Gated Delivery

一套面向软件代理的有限交付工作约定：明确可观察的结果，在授权内连续实现，用独立预期和实际检查决定是否完成。`gated-delivery` 是本技能的稳定标识，不表示成熟度等级或认证。

适用于明确的软件实现、缺陷修复、有限验收和中断续做。简单编辑采用最小检查；复杂或有副作用的任务再使用相应专题，不把每项工作都变成完整审计。

## 从哪里开始

- [技能入口](skills/gated-delivery/SKILL.md)：按任务选择必要规则。
- [设计原则](skills/gated-delivery/references/design-principles.md)：结果、反例、证据与结束条件。
- [执行与验证环境](skills/gated-delivery/references/harness.md)：复用项目现有工具、上下文和权限。
- [需求澄清与复用](skills/gated-delivery/references/requirements-and-reuse.md)：先查现状，消除关键歧义，保护原有行为。
- [技能分工](docs/skill-map.md)：通用方法与项目知识分开维护。

## 本地验证

```sh
python3 scripts/check_repo.py --public
python3 -m unittest discover -s tests -v
```

需要结构化检查记录时使用 [验证合同](docs/verification-contract.md)。工具核对来源、日志、计数与覆盖关系，不判断业务断言是否充分，也不授予发布或生产操作权限。已有项目测试优先，低影响修改不必建立大型矩阵。

当前候选版本为 **2.3.0**。安装器只处理 `gated-delivery`，不会删除目标中其它技能或链接；操作方式见 [安装说明](docs/installation.md)。项目领域知识留在各自项目，分发包不包含客户资料、私有事故、原始教材或运行凭据。

## 公开前的范围确认

[方法与资料边界](docs/method-scope.md)说明哪些内容属于本仓库的通用约定；[公开检查清单](docs/publication.md)区分当前文件检查、历史与作者身份、资料权利、许可证及实际发布。命名调整不代替来源和分发范围审查。

本项目采用 MIT 许可证，详见 [LICENSE](LICENSE)。文件检查不代表远端强制合并保护已建立。
