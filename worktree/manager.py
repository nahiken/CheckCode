"""Git Worktree 生命周期管理 + Diff/变更文件清单。

职责:
- create:在 repo 上创建独立 worktree(新分支),返回 Worktree 描述。
- remove:拆除 worktree + 删除分支,容忍已不存在(幂等清理)。
- base_commit / diff / changed_files:任务基线与本任务修改的取证。

隔离原理:worktree 共享主仓库的对象库(`.git` 文件指向主仓 .git/worktrees/),
但工作树独立。主仓库工作树从不暴露给 Agent——沙箱以 worktree 路径为界。
"""

from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path


class WorktreeError(RuntimeError):
    """git worktree 操作失败(含 git 命令非零退出)。"""


@dataclass(frozen=True)
class Worktree:
    """一个已创建的隔离 worktree。"""

    path: Path  # worktree 工作目录
    branch: str  # worktree 所在分支名


class WorktreeManager:
    """在给定 git 仓库上管理 worktree 生命周期与 Diff 取证。"""

    def __init__(self, repo: Path) -> None:
        if not (repo / ".git").exists() and not repo.joinpath(".git").is_dir():
            raise WorktreeError(f"不是 git 仓库: {repo}")
        self.repo = repo

    def _git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        """在 cwd 执行 git 命令并解码为 UTF-8 文本。

        用 bytes 模式 + 显式 UTF-8 解码:git 在 Windows 控制台默认 GBK,
        仓库内中文内容(README/注释)会触发 'gbk' codec 崩溃。errors='replace'
        保证任何字节都能被读取(与 M2 run_command 同策略)。
        """
        p = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=False,
            check=False,
        )
        return subprocess.CompletedProcess(
            args=p.args,
            returncode=p.returncode,
            stdout=p.stdout.decode("utf-8", errors="replace"),
            stderr=p.stderr.decode("utf-8", errors="replace"),
        )

    def base_commit(self) -> str:
        """当前 HEAD(作为任务基线)。"""
        p = self._git(self.repo, "rev-parse", "HEAD")
        if p.returncode != 0:
            raise WorktreeError(f"rev-parse HEAD 失败: {p.stderr.strip()}")
        return p.stdout.strip()

    def audit_path(self, branch: str) -> Path:
        """该 worktree 分支的审计日志路径(单一来源)。

        落盘在 worktree 之外(repo/.git/whycode-audits/):worktree 是临时工作区,
        拆除后审计仍须可回溯。`.git/` 下既保留又不进主仓工作树。
        """
        return self.repo / ".git" / "whycode-audits" / f"{branch}.jsonl"

    def session_path(self, branch: str) -> Path:
        """该分支的会话持久化文件路径(单一来源,与审计同构)。

        落盘在 worktree **之外**(repo/.git/whycode-sessions/):会话记录是运行期
        产物,worktree 拆除后仍须可恢复;放 `.git/` 下既不进主仓工作树(git status
        恒净,不污染「零污染」验收),又能被 `mewcode resume` 按 session 定位。
        M4 挂接点。
        """
        return self.repo / ".git" / "whycode-sessions" / f"{branch}.jsonl"

    def _default_path(self, branch: str) -> Path:
        """create_from_branch 的 worktree 路径公式(单一来源):主仓兄弟目录 + 分支后缀。

        create 的路径用随机 uuid 后缀(每次新建必唯一);此处用分支名里的 session
        随机后缀(removeprefix("sess-") 后取尾 10 位)——同一分支映射同一路径,
        可幂等重建/复用。两处公式同源(都是「兄弟目录 + 随机后缀」)。
        """
        suffix = branch.removeprefix("sess-")[-10:]
        return self.repo.parent / f"{self.repo.name}-wt-{suffix}"

    def create(
        self,
        *,
        branch: str | None = None,
        path: Path | None = None,
    ) -> Worktree:
        """创建隔离 worktree(新分支)。branch 缺省自动唯一命名,避免并行冲突。

        path 缺省取主仓库的兄弟目录(如 ../demo_repo-wt-<suffix>):
        - 必须与主仓工作树同盘(symlink 的 .git 指向主仓对象库,Windows 跨盘受限);
        - 不能在主仓工作树内部(git 拒绝嵌套 checkout,且会污染主仓 status)。

        路径用随机后缀:每次新建必唯一,同分支重复 create 不撞路径(分支冲突由
        worktree add 阶段报错,见 test_create_live_conflict_raises)。
        """
        suffix = uuid.uuid4().hex[:10]
        branch = branch or f"sess-{suffix}"
        path = path or self.repo.parent / f"{self.repo.name}-wt-{suffix}"
        path = path.resolve()
        if path.exists():
            raise WorktreeError(f"worktree 路径已存在: {path}")
        p = self._git(self.repo, "worktree", "add", "-q", "-b", branch, str(path))
        if p.returncode != 0:
            # 前次运行崩溃可能残留:同名 worktree 路径 + 孤儿分支。
            # 清理(路径、分支)后重试一次;若分支仍被活动 worktree 占用则失败。
            self._git(self.repo, "worktree", "remove", "-f", str(path))
            self._git(self.repo, "branch", "-D", branch)
            p = self._git(self.repo, "worktree", "add", "-q", "-b", branch, str(path))
            if p.returncode != 0:
                raise WorktreeError(
                    f"worktree add 失败(分支 {branch} 可能已被活动 worktree 占用): "
                    f"{p.stderr.strip()}"
                )
        return Worktree(path=path, branch=branch)

    def attach(self, branch: str, *, path: Path | None = None) -> Worktree:
        """把 worktree 挂到**既有分支**(不新建分支),返回其描述。

        用途:闭环结束后 worktree 已拆除但**分支保留**(M4 恢复锚点),分支上带着
        Agent 的提交。要在「改造后的最终态」上做验收/复核,就必须 checkout 那条分支
        —— 而主工作树停在基线(闭环刻意不碰它,见 `force_branch`)。本方法提供与
        `create` 对称的取证入口:同样是独立工作树,只是分支已存在。

        与 `create` 的三点差异:
        - `git worktree add <path> <branch>`(**不带 `-b`**)—— 不新建分支;
        - 该分支不能已被别的活动 worktree 占用(git 会拒绝,向上抛 WorktreeError);
        - 调用方负责用 `remove(...)` 拆除(此处不删分支 —— 它不属于本方法)。

        路径缺省同样取主仓兄弟目录的随机后缀,避免与 `create` 的路径相撞。
        """
        suffix = uuid.uuid4().hex[:10]
        path = path or self.repo.parent / f"{self.repo.name}-wt-{suffix}"
        path = path.resolve()
        if path.exists():
            raise WorktreeError(f"worktree 路径已存在: {path}")
        p = self._git(self.repo, "worktree", "add", "-q", str(path), branch)
        if p.returncode != 0:
            # 同 create:清理可能残留的同路径条目后重试一次
            self._git(self.repo, "worktree", "remove", "-f", str(path))
            p = self._git(self.repo, "worktree", "add", "-q", str(path), branch)
            if p.returncode != 0:
                raise WorktreeError(
                    f"worktree attach 失败(分支 {branch} 可能已被活动 worktree 占用): "
                    f"{p.stderr.strip()}"
                )
        return Worktree(path=path, branch=branch)

    def remove(self, wt: Worktree, *, keep_branch: bool = False) -> None:
        """拆除 worktree;已不存在则静默(幂等)。

        keep_branch=True:仅拆 worktree 路径,**保留分支**(供会话恢复时复用——M4)。
        """
        self._git(self.repo, "worktree", "remove", "-f", str(wt.path))
        if not keep_branch:
            # 若分支已被手动合并/删除,容忍失败
            self._git(self.repo, "branch", "-D", wt.branch)

    def create_from_branch(self, branch: str, path: Path | None = None) -> Worktree:
        """从既有分支重建 worktree(会话恢复:M4 复用已完成写操作的分支)。

        worktree 已注册(残留)→ 幂等复用;未注册 → `worktree add <branch> <path>`。
        path 缺省用 _default_path(与 create 同公式,单一来源)。
        用于恢复时**不重放副作用**:写操作已提交到 branch,直接检出现状继续。
        """
        if path is None:
            path = self._default_path(branch)
        p = self._git(self.repo, "worktree", "list", "--porcelain")
        if f"branch refs/heads/{branch}" in p.stdout and str(path) in p.stdout:
            return Worktree(path=path, branch=branch)
        path = path.resolve()
        p = self._git(self.repo, "worktree", "add", "-q", str(path), branch)
        if p.returncode != 0:
            raise WorktreeError(
                f"从分支重建 worktree 失败({branch}): {p.stderr.strip()}"
            )
        return Worktree(path=path, branch=branch)

    def diff(self, base: str, wt: Worktree) -> str:
        """worktree 分支相对基线的完整 Diff。

        三-dot:diff <base>...<branch> = 从 base 分出后 branch 的累积修改
        (与 merge-base 比较,只含本分支新增的改动)。
        """
        p = self._git(self.repo, "diff", f"{base}...{wt.branch}")
        if p.returncode != 0:
            raise WorktreeError(f"diff 失败: {p.stderr.strip()}")
        return p.stdout

    def changed_files(self, base: str, wt: Worktree) -> list[str]:
        """本任务修改的文件清单(相对仓库根,按 diff 顺序)。"""
        p = self._git(self.repo, "diff", "--name-only", f"{base}...{wt.branch}")
        if p.returncode != 0:
            raise WorktreeError(f"diff --name-only 失败: {p.stderr.strip()}")
        return [line for line in p.stdout.splitlines() if line.strip()]

    def commit_all(self, wt: Worktree, message: str) -> bool:
        """提交 worktree 的全部工作区改动。无改动 → 返回 False(不报错)。

        用 pathspec 排除运行时噪声,避免它们进入 Diff/风险报告:
        - `__pycache__/` + `*.pyc`:验证(跑 pytest)产生的字节码。
        - `.whycode/`:审计日志已外置到 repo/.git/whycode-audits/(M3 review 修复),
          此排除仅作防御,防未来其它产物落进工作区。
        不用 .gitignore(会作为新文件被提交进 diff);pathspec 排除即可。

        使用显式 user 配置(git init 的仓库无全局 user.name/email,直接 commit 会失败)。
        """
        p = self._git(
            wt.path,
            "add", "-A", "--",
            ".", ":(exclude).whycode", ":(exclude)__pycache__", ":(exclude)*.pyc",
        )
        if p.returncode != 0:
            raise WorktreeError(f"git add 失败: {p.stderr.strip()}")
        # 无任何 staged 变更时 git commit 报 "nothing to commit"
        p = self._git(
            wt.path,
            "-c", "user.name=whycode",
            "-c", "user.email=whycode@local",
            "commit", "-q", "-m", message,
        )
        return p.returncode == 0

    def force_branch(self, branch: str, commit: str) -> None:
        """强制把分支指向指定提交(丢弃该分支上的后续 commit)。

        回滚语义:任务分支已含 Agent commit(commit_all 落分支)→ 重置分支
        HEAD 到基线,丢弃全部 Agent 改动。不 checkout(主工作树保持原分支)。
        """
        p = self._git(self.repo, "branch", "-f", branch, commit)
        if p.returncode != 0:
            raise WorktreeError(f"git branch -f 失败: {p.stderr.strip()}")
