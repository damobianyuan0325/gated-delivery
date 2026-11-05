# 可执行验证合同

工具只提供证据完整性门禁，不是业务真相判定器，也不授予生产操作权限。人工/独立审阅仍须核对测试预期是否来自需求、实际入口和适配器是否达到声明层级。

适用：需要可追踪交付凭据、回归 RED/GREEN 或 CI 覆盖核对的任务。普通问答和低影响修改可直接用原有工具，不必生成这份合同。

## 计划与运行

从 [示例计划](../skills/gated-delivery/assets/delivery-plan.example.json) 复制到项目，再将示例路径、明确命令和标准改为该项目实际值。

- `task_id`、A/T ID 稳定；`criteria` 是应发生/不应发生的可观察条件，`test_ids` 关联验证。
- `source_files` 是最终验证必须绑定的明确文件，可包含尚待实现的文件；缺席会被记录。`oracle_files` 是独立预期/测试文件，属于前者子集。
- `checks.command` 是 argv 数据；读取凭据不会执行它。操作者另以显式 CLI 参数运行同一命令。发现资料中的命令仍须先确认是否符合任务授权。
- `level` 声明 unit/integration/ui/production；工具校验声明与覆盖一致，不能自动证明真实环境层级。
- `kind=test` 必须有 unittest 或 JUnit 格式报告；工具重算归档报告中的计数，零测试、失败、错误、skip/xfail 不满足必做门禁。它不能从日志证明框架或断言实际执行，需审阅可信命令和测试源码；任意打印兼容格式的程序不因此变成测试。lint 等非测试命令用 `kind=command`。
- 有副作用的任务设置 `side_effecting=true`，列出已授权动作、保护项、停止条件并包含必做反向标准；字段存在不等于授权已获证。

```sh
python3 /path/to/gated-delivery/scripts/run_check.py \
  --root /path/to/project --plan delivery-plan.json --check T1 \
  -- python3 -m unittest discover -s tests -v

python3 /path/to/gated-delivery/scripts/check_delivery.py \
  --root /path/to/project --plan delivery-plan.json
```

每次运行产生新的 `evidence/<check>/runs/<run>/`，保存原 stdout/stderr、实际退出码、报告计数、耗时及源码摘要。失败不覆盖；`latest.json` 必须指向按生成名称排序的最新目录，未完成的新目录也阻止回用旧成功。两路日志各有1MiB的子进程文件上限，触及上限即拒绝，合计至多2MiB；这也约束该检查写出的其它普通文件。计划中的检查超时是本地测试限制，不是产品时限承诺。

本 runner 使用 POSIX 独立进程组，停止自己启动的超时检查；它不是生产进程管理器，也不保证管理主动脱离进程组的后台守护程序。其它系统采用项目原生 runner，不能宣称此脚本已经适配。

## 回归与拒绝

已证缺陷可设置 `criteria.requires_red=true`。门禁要求先有同一测试预期的真实断言失败，再有最终源码 GREEN。仅换实现文件可以；改变 oracle、把夹具 ERROR 当业务 RED、零方法或修改日志不能满足该条件。已有行为或文档维护不强制制造失败，也不要求旧套件全部变红。

`check_delivery.py` 只读核对：计划/argv/层级/ID、原日志摘要、真实计数、最终源码、A/T覆盖以及声明的 RED。它拒绝源码变动、伪造计数、缺证、最后一次失败和越界路径。不把一个组件通过升级为整个功能、生产环境、所有平台或真实业务效果通过。

JUnit 原报告在每次运行后重新核对路径并独立归档；不接纳旧报告或执行中新建的符号链接。凭据不是密码学远程证明：有写权限的人仍能重造所有文件、时钟或日志；CI应在可信运行器重新执行经过审阅的测试，不能只上传一包自称通过的 JSON。报告解析通过不证明框架来源、断言充分或实际方法数。

## CI 与主干保护

仓库工作流运行结构校验和实际工具测试。工作流文件存在、Actions确实运行成功、以及 GitHub required checks/ruleset真正禁止合并，是三个不同的证据。

远端保护需要账户计划和仓库权限支持。不要为获得该能力自动升级付费计划；若无法启用，明确记录 `NOT_ENFORCED`，不要宣称已实现“失败绝不能合并”。本地 gate 的失败退出演示也不能替代 GitHub 服务端阻断证明。
