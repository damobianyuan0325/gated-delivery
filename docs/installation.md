# 安装通用研发技能

安装器只接受 `gated-delivery`，来源为本仓库的 `skills/`。领域技能由对应项目维护，本安装器不复制或删除它们。不会重写 bundled／plugin 技能、其他技能、目标内源清单之外的文件，也不访问网络、Git 或数据库。这里只给操作方法；本次是否已实际安装，以执行方的真实结果为准。文件安装不证明未来行为已经采用，也不保证当前对话已重新加载技能。

## 先生成冻结计划，目标不写入

使用 Python 3.9+，仅需标准库。将 `REPO`、`TARGET`、`BUNDLE` 设为真实绝对路径；不能带 `..` 或路径别名。`BUNDLE` 必须是仓库和目标根之外尚不存在的独立目录，其父目录须存在。

```sh
python3 "$REPO/scripts/install_skills.py" \
  --source-root "$REPO/skills" \
  --target-root "$TARGET" \
  --backup-root "$BUNDLE"
```

默认只有 prepare／dry-run：读取来源及目标，创建独立备份目录和 `plan.json`／`plan.sha256`。目标根可以尚不存在，但其父目录须存在。计划固定 `gated-delivery` 的文件／目录域、源 SHA／模式、目标每个计划文件的 before 存在性／原字节 SHA／大小／模式和独立备份，另列该技能域内源外目标文件。原件以独立文件保存，不是指向仍会变化目标的链接；冻结源副本也保留在 bundle。不会因 dry-run 自动安装。

`--skill gated-delivery` 可显式指定唯一白名单项；省略时也只选择它。重复选择、曾经支持的领域技能名及其他名称都会拒绝。若当前技能位置是链接，应将目标设为它所在的真实 canonical 根，而非让安装器沿链接写入：

```sh
python3 "$REPO/scripts/install_skills.py" --source-root "$REPO/skills" \
  --target-root "$CANONICAL_ENGINEERING_ROOT" --backup-root "$ENGINEERING_BUNDLE" \
  --skill gated-delivery
```

每个计划固定选择范围，apply／restore 只接受该计划，不能另加 `--skill` 改域。其他技能不遍历、不创建、不改写；其他名称下的现有链接保持原样。选中技能及其父链仍必须是 canonical 实目录，任何选中链接／别名仍拒绝。旧版覆盖领域技能的计划须保留其冻结工具与原 bundle，不能交给当前更窄安装器重解释、清日志或重放；本次仓库分离不是撤销过去的安装，也不是全局卸载。

检查计划的真实源／目标／备份路径、before 原件、变更及保留文件后再应用。`plan.json` 是具体清单，不是通用批准；源、目标 before、备份或工具改变即拒绝，须保留旧包并重新准备新目录，不能修改旧 seal 求通过。瞬时 Python 缓存、`.DS_Store` 和 `.git` 不属于技能 payload。计划／备份目录不应加入仓库或上传。

## 显式应用，禁止成功重放

```sh
python3 "$REPO/scripts/install_skills.py" --apply --plan "$BUNDLE/plan.json"
```

首写前重核源全域、目标 before 与独立备份，并同步备份文件／目录。按文件独占暂存和原子替换；已有目录保持原模式，新目录使用计划模式。before 不符、链接／别名／重叠路径、未知文件类型、源变化或读回不符会拒绝或进入恢复。成功后计划文件字节／模式须全部匹配，`WAL.jsonl` 保留提交记录；同包成功或失败均不自动重新 apply。

## 失败与中断

捕获到应用失败时自动恢复本次已知目标：暂存文件落盘后、目标替换前，WAL 先落盘记录其设备号／inode；只有当前文件仍是本次暂存 inode 且字节／模式为已知 after，才恢复独立 before。当前已经是 before 则不动；未触达的计划路径或被他人换成新 inode 的文件，即使内容恰好等于 after，也保留并报告冲突。原 absent 文件只删除能证明由本次替换产生的文件。新目录仅按本次创建记录、归属和空目录条件逆序 `rmdir`，绝不递归删除未知内容。保留原计划、备份、失败和 WAL；`RESTORE_PARTIAL_PRESERVED_UNKNOWN` 表示仍需人工核对，不冒称完整恢复。inode／ctime 无法由普通副本恢复，恢复范围以文件字节／模式及可恢复时间为准。

进程被强制结束或断电不等于已自动恢复。先检查原 WAL 与当前文件状态，不能再次 apply 试运气。对于尚无成功提交且尚未执行恢复的已知完整 WAL，可显式恢复：

```sh
python3 "$REPO/scripts/install_skills.py" --restore --plan "$BUNDLE/plan.json"
```

不自动回滚已成功安装；部分／损坏 WAL、已恢复或有陌生内容时保留证据，不清日志重试。以后若需要撤销一次成功安装，应另行准备有当前 before 的明确计划。

## 临时根验证

```sh
python3 -B "$REPO/tests/test_install.py" -v
```

测试只在安装器创建的 `tempfile.TemporaryDirectory` 中运行，验证 dry-run／独立 before、完整文件形状、末项已替换后故障及日志失败恢复、持久化归属恢复、并发同内容但未归属文件保护、未知文件／模式保护、源与目标漂移拒绝和成功不重放。另将仓库当前真实 `gated-delivery` 全部payload复制到临时源，验证完整安装及末项失败的恢复形状；领域技能选择必须拒绝，目标中已有领域技能及链接须保持。故障入口不在 CLI 暴露，需要活跃临时根能力；真实 `.codex` 目标禁止注入。测试通过只证明这些隔离场景，不等于实际安装、当前会话重载或后续任务行为已经通过。

## 安装与公开分发不同

安装器会保留目标中的未管理文件。已安装目录、旧安装包和恢复备份都可能包含源清单外内容，不应用它们直接制作公开压缩包。对外分发从经过审阅的当前源码清单构建独立候选，再核对候选本身；文件保留不等于已经完成公开审查。
