#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# AlterEgo · 架构红线检查
#
# 这些检查强制保证「内核无知」（原则 P1）与分层依赖方向。
# 任何一条失败都意味着架构被破坏，CI 会直接失败。
#
# 用法：
#   bash scripts/check_architecture.sh
#   bash scripts/check_architecture.sh --verbose
#
# 退出码：
#   0 = 全部通过
#   1 = 存在架构违规
# ─────────────────────────────────────────────────────────────

set -uo pipefail

VERBOSE=0
[[ "${1:-}" == "--verbose" ]] && VERBOSE=1

SRC="src/alterego"
KERNEL="$SRC/kernel"
DOMAIN="$SRC/domain"
SIM="$SRC/sim"
STORAGE="$SRC/storage"
LLM="$SRC/llm"

FAILED=0
CHECKS=0

# 颜色（非 TTY 时自动禁用）
if [[ -t 1 ]]; then
    RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'
    CYAN=$'\033[36m'; BOLD=$'\033[1m'; RESET=$'\033[0m'
else
    RED=""; GREEN=""; YELLOW=""; CYAN=""; BOLD=""; RESET=""
fi

# ─────────────────────────────────────────────────────────────
# 辅助函数
# ─────────────────────────────────────────────────────────────

# check_forbidden <名称> <正则> <路径> <说明>
check_forbidden() {
    local name="$1" pattern="$2" path="$3" hint="$4"
    CHECKS=$((CHECKS + 1))

    [[ -d "$path" ]] || { printf '  %s⊘%s %s  %s(目录不存在，跳过)%s\n' \
        "$YELLOW" "$RESET" "$name" "$YELLOW" "$RESET"; return; }

    local hits
    hits=$(grep -rEn --include='*.py' "$pattern" "$path" 2>/dev/null || true)

    if [[ -n "$hits" ]]; then
        printf '  %s✗%s %s\n' "$RED" "$RESET" "$name"
        while IFS= read -r line; do
            printf '      %s%s%s\n' "$YELLOW" "$line" "$RESET"
        done <<< "$hits"
        printf '      %s修复建议: %s%s\n' "$CYAN" "$hint" "$RESET"
        FAILED=$((FAILED + 1))
    else
        printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$name"
        [[ $VERBOSE -eq 1 ]] && printf '      %s无匹配 (pattern: %s in %s)%s\n' \
            "$CYAN" "$pattern" "$path" "$RESET"
    fi
}

# check_required <名称> <正则> <路径> <说明>
check_required() {
    local name="$1" pattern="$2" path="$3" hint="$4"
    CHECKS=$((CHECKS + 1))

    [[ -d "$path" ]] || return

    local hits
    hits=$(grep -rEn --include='*.py' "$pattern" "$path" 2>/dev/null || true)

    if [[ -z "$hits" ]]; then
        printf '  %s✗%s %s\n' "$RED" "$RESET" "$name"
        printf '      %s修复建议: %s%s\n' "$CYAN" "$hint" "$RESET"
        FAILED=$((FAILED + 1))
    else
        printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$name"
    fi
}

# check_forbidden_excluding <名称> <正则> <路径> <排除的路径片段> <说明>
#
# 用于「除了实现该机制的那个文件，其他任何地方都不准这么做」的规则。
# ``exclude`` 是**扩展正则**，可写 ``a\.py|/b/`` 一次排除多处。
check_forbidden_excluding() {
    local name="$1" pattern="$2" path="$3" exclude="$4" hint="$5"
    CHECKS=$((CHECKS + 1))

    [[ -d "$path" ]] || { printf '  %s⊘%s %s  %s(目录不存在，跳过)%s\n' \
        "$YELLOW" "$RESET" "$name" "$YELLOW" "$RESET"; return; }

    local hits
    hits=$(grep -rEn --include='*.py' "$pattern" "$path" 2>/dev/null \
           | grep -vE "$exclude" || true)

    if [[ -n "$hits" ]]; then
        printf '  %s✗%s %s\n' "$RED" "$RESET" "$name"
        while IFS= read -r line; do
            printf '      %s%s%s\n' "$YELLOW" "$line" "$RESET"
        done <<< "$hits"
        printf '      %s修复建议: %s%s\n' "$CYAN" "$hint" "$RESET"
        FAILED=$((FAILED + 1))
    else
        printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$name"
    fi
}

# check_file_size <路径> <上限行数>
check_file_size() {
    local limit="$1"
    CHECKS=$((CHECKS + 1))
    local bad=""

    while IFS= read -r f; do
        local n
        n=$(wc -l < "$f")
        if (( n > limit )); then
            bad+="      $f ($n 行)"$'\n'
        fi
    done < <(find "$SRC" -name '*.py' -not -path '*/migrations/*' 2>/dev/null)

    if [[ -n "$bad" ]]; then
        printf '  %s✗%s 单文件行数 ≤ %s\n' "$RED" "$RESET" "$limit"
        printf '%s' "$bad"
        printf '      %s修复建议: 拆分模块，把职责分离到多个文件%s\n' "$CYAN" "$RESET"
        FAILED=$((FAILED + 1))
    else
        printf '  %s✓%s 单文件行数 ≤ %s\n' "$GREEN" "$RESET" "$limit"
    fi
}

# ─────────────────────────────────────────────────────────────
printf '\n%s%s AlterEgo 架构红线检查 %s\n\n' "$BOLD" "$CYAN" "$RESET"

# ═════════════════════════════════════════════════════════════
# 第 1 组 · 内核无知（原则 P1）
#
# 内核层绝不能知道任何具体实现：
#   不知道用什么数据库、什么 LLM、什么 IM 平台、什么 Web 框架
# ═════════════════════════════════════════════════════════════
printf '%s【第 1 组】内核无知 —— kernel/ 不得引用任何具体技术%s\n' "$BOLD" "$RESET"

check_forbidden \
    "kernel/ 不引用具体存储与外部服务" \
    "sqlite|openai|anthropic|wecom|dingtalk|telegram|fastapi" \
    "$KERNEL" \
    "内核只定义 Protocol 与抽象。把具体实现移到 storage/ llm/ channels/。"

check_forbidden \
    "kernel/ 不引用第三方 HTTP 客户端" \
    "\b(requests|httpx|aiohttp|urllib3)\b" \
    "$KERNEL" \
    "HTTP 是外部渠道的实现细节。内核不该知道网络的存在。"

check_forbidden \
    "kernel/ 不直接连数据库" \
    "\bsqlite3?\b|psycopg|pymysql" \
    "$KERNEL" \
    "内核通过 StorageBackend Protocol 访问存储。"

check_forbidden \
    "kernel/ 不引用任何上层模块" \
    "from alterego\.(domain|sim|storage|llm|channels|capabilities|npc)" \
    "$KERNEL" \
    "依赖方向是单向的：Interfaces → Simulation → Domain → Kernel。内核在最低层，不能反向依赖。"

# ═════════════════════════════════════════════════════════════
# 第 2 组 · 领域层纯净
#
# 领域层是纯函数式的业务逻辑，不得有副作用与 IO
# ═════════════════════════════════════════════════════════════
printf '\n%s【第 2 组】领域层纯净 —— domain/ 必须是纯函数，无 IO%s\n' "$BOLD" "$RESET"

check_forbidden \
    "domain/ 不做文件 IO" \
    "[^_a-zA-Z]open\(" \
    "$DOMAIN" \
    "领域层接收数据、返回数据，不做 IO。"

check_forbidden \
    "domain/ 不访问数据库与网络" \
    "\bsqlite3?\b|\b(requests|httpx|aiohttp)\b" \
    "$DOMAIN" \
    "领域层是纯逻辑，持久化由 Repository 在更上层完成。"

check_forbidden \
    "domain/ 不调用 LLM" \
    "from alterego\.llm|import alterego\.llm" \
    "$DOMAIN" \
    "领域层不知道 LLM 的存在。需要 LLM 的逻辑放在 sim/ 或 capabilities/。"

# 例外：persona.py 需要读取模板？不，模板由调用方传入。
check_forbidden \
    "domain/ 不直接读环境变量" \
    "os\.environ|os\.getenv" \
    "$DOMAIN" \
    "所有配置经 config 层注入，领域层不读环境。"

# ═════════════════════════════════════════════════════════════
# 第 3 组 · 推演层抽象
#
# sim/ 只能通过 Protocol 访问存储与 LLM，不能绑定具体实现
# ═════════════════════════════════════════════════════════════
printf '\n%s【第 3 组】推演层通过接口访问外部 —— sim/ 不得绑定具体实现%s\n' "$BOLD" "$RESET"

check_forbidden \
    "sim/ 不直接依赖 sqlite 实现" \
    "from alterego\.storage\.sqlite|import alterego\.storage\.sqlite" \
    "$SIM" \
    "通过 Repository Protocol 或 ctx.storage 访问，不 import 具体实现。"

# 上面那条只盯 sim/，而 `storage/sqlite/__init__.py` 的 docstring 承诺的是
# 「其他层一律不得 import」。承诺与检查范围不一致时，承诺就是一句空话——
# 所以这里把它扩到整个 src/，只留两个正当的例外：
#
#   cli.py      组装根（composition root）。它的职责就是挑一个具体实现装上，
#               和 `main()` 里 `Main->>Store: migrate()`（01-architecture.md § 6.1）
#               是同一件事。组装根引用具体实现是架构允许的，不是漏洞。
#   cli_db.py   同一件事的延伸：`alterego db` 那几个维护命令住在这里。
#               它是因为 cli.py 撞上 900 行上限才拆出去的，职责没变。
#   cli_memory.py  同理，`alterego memory` 的两条梳理命令。
#               组装根多几个不要紧——要紧的是**别再冒出第四个地方**。
#   storage/    实现自己。`backend.py` / `migrator.py` 当然要互相 import。
check_forbidden_excluding \
    "sqlite 实现只被组装根与存储层引用" \
    "from alterego\.storage\.sqlite|import alterego\.storage\.sqlite" \
    "$SRC" \
    "cli(_db|_memory)?\.py:|/storage/" \
    "除 cli.py / cli_db.py / cli_memory.py（组装根）与 storage/ 之外，一律通过 StorageBackend Protocol 访问。想要具体实现，让组装根构造好再传进来。"

check_forbidden \
    "sim/ 不直接依赖具体 LLM 客户端" \
    "from alterego\.llm\.client|import alterego\.llm\.client" \
    "$SIM" \
    "通过 LLMProvider Protocol 或 ctx.llm() 访问。"

check_forbidden \
    "sim/ 不直接调用 IM 平台" \
    "\b(wecom|dingtalk|telegram)\b" \
    "$SIM" \
    "推送由 Channel Protocol 完成。sim/ 只产生 OutboundMessage。"

# 推演层必须是可复现的：禁止全局随机与真实时钟
check_forbidden \
    "sim/ 不使用全局 random（破坏可复现性）" \
    "import random$|random\.(random|randint|choice|shuffle|uniform)\(" \
    "$SIM" \
    "必须使用 ctx.rng（tick 内固定种子），否则同一 tick 重放结果不同。"

check_forbidden \
    "sim/ 不使用真实时钟（破坏虚拟时间）" \
    "datetime\.now\(|time\.time\(|datetime\.utcnow\(" \
    "$SIM" \
    "必须使用 ctx.now() / ctx.clock / ctx.virtual_now。"

# ═════════════════════════════════════════════════════════════
# 第 4 组 · 分层不得越级
# ═════════════════════════════════════════════════════════════
printf '\n%s【第 4 组】分层不得越级%s\n' "$BOLD" "$RESET"

# 注意 domain 不在禁止列表里。
#
# `01-architecture.md § 1.1` 的依赖矩阵明确写着 `storage → domain` 是允许的：
# `03-data-model.md § 7` 的 Repository 要返回 `Persona` / `Memory` 等 domain 类型，
# 而 Repository 的实现就在这里。禁止它会让 § 7 的契约无法实现。
#
# 「不含业务逻辑」由此检查剩下的部分 + 代码评审保证：
# 存储层可以**拿 domain 的类型**，但不准**调 domain 的规则**去改写数据。
check_forbidden \
    "storage/ 不含业务逻辑" \
    "from alterego\.(sim|channels|capabilities)" \
    "$STORAGE" \
    "存储层只做数据搬运，不知道业务规则。可以依赖 domain 取类型形状（见 01-architecture.md § 1.1 依赖矩阵），但不得依赖 sim/channels/capabilities。"

check_forbidden \
    "llm/ 不含业务逻辑" \
    "from alterego\.(sim|domain|channels|capabilities|storage)" \
    "$LLM" \
    "LLM 层只做协议适配、重试、计量。"

# ═════════════════════════════════════════════════════════════
# 第 5 组 · 通用卫生
# ═════════════════════════════════════════════════════════════
printf '\n%s【第 5 组】通用代码卫生%s\n' "$BOLD" "$RESET"

check_forbidden \
    "生产代码不留 print" \
    "^\s*print\(" \
    "$SRC" \
    "使用 logging（在插件中用 ctx.logger）。print 只允许出现在 CLI 输出与 channels/console。"

# 红线 5（v0.2.0 新增）：不得自行构造 logging handler。
#
# 理由：密钥脱敏（SecretFilter）与文件轮转都挂在 **root logger** 上。
# 自建 handler 的代码看上去完全正常，但密钥会在那一天直接写进日志。
# 例外只有 kernel/logging.py —— 它就是那个唯一被允许构造 handler 的地方。
check_forbidden_excluding \
    "不自行构造 logging handler（脱敏会失效）" \
    "logging\.(FileHandler|StreamHandler|RotatingFileHandler|TimedRotatingFileHandler|basicConfig\()" \
    "$SRC" \
    "kernel/logging.py" \
    "用 getLogger(__name__) / ctx.logger；脱敏、轮转、格式都由 kernel/logging.py 统一负责。"

check_file_size 900

# 真正检查 __init__.py 的存在
CHECKS=$((CHECKS + 1))
MISSING_INIT=""
while IFS= read -r d; do
    if [[ ! -f "$d/__init__.py" ]] && [[ "$(find "$d" -maxdepth 1 -name '*.py' | head -1)" != "" ]]; then
        MISSING_INIT+="      $d"$'\n'
    fi
done < <(find "$SRC" -type d -not -path '*/__pycache__*' 2>/dev/null)

if [[ -n "$MISSING_INIT" ]]; then
    printf '  %s✗%s 所有含 .py 的目录都有 __init__.py\n' "$RED" "$RESET"
    printf '%s' "$MISSING_INIT"
    FAILED=$((FAILED + 1))
else
    printf '  %s✓%s 所有含 .py 的目录都有 __init__.py\n' "$GREEN" "$RESET"
fi

# ═════════════════════════════════════════════════════════════
# 第 6 组 · 插件自包含
# ═════════════════════════════════════════════════════════════
printf '\n%s【第 6 组】插件不得互相依赖%s\n' "$BOLD" "$RESET"

CHECKS=$((CHECKS + 1))
if [[ -d "plugins" ]]; then
    CROSS=""
    for pdir in plugins/*/; do
        [[ -d "$pdir" ]] || continue
        pname=$(basename "$pdir")
        [[ "$pname" == .* ]] && continue
        hits=$(grep -rEn --include='*.py' "from plugins\.|import plugins\." "$pdir" 2>/dev/null \
               | grep -v "plugins\.$pname" || true)
        [[ -n "$hits" ]] && CROSS+="$hits"$'\n'
    done

    if [[ -n "$CROSS" ]]; then
        printf '  %s✗%s 插件之间不得直接 import\n' "$RED" "$RESET"
        printf '%s' "$CROSS"
        printf '      %s修复建议: 通过 ctx.registry 获取服务，不要跨插件 import%s\n' "$CYAN" "$RESET"
        FAILED=$((FAILED + 1))
    else
        printf '  %s✓%s 插件之间不得直接 import\n' "$GREEN" "$RESET"
    fi
else
    printf '  %s⊘%s 插件之间不得直接 import  %s(plugins/ 不存在，跳过)%s\n' \
        "$YELLOW" "$RESET" "$YELLOW" "$RESET"
fi

# ═════════════════════════════════════════════════════════════
# 第 7 组 · LLM 调用必经 ctx.llm()（红线 7，v0.2.0 新增）
#
# 直接用 httpx / openai SDK 调端点看上去能跑，但会同时破坏三件事：
#   1. 计量   —— 这一次调用不会进 llm_usage，成本统计漏报
#   2. 路由   —— 绕过了 [llm.routing] 的分层，用错档位的模型
#   3. 闸门   —— 绕过了 [llm.budget]，预算上限形同虚设
# 三个后果都要等到**收到账单**那天才显形。
# ═════════════════════════════════════════════════════════════
printf '\n%s【第 7 组】LLM 调用必经 ctx.llm()%s\n' "$BOLD" "$RESET"

check_forbidden \
    "sim/ 不直连 LLM 端点" \
    "httpx\.(Async)?Client\(.*base_url|openai\.(Async)?OpenAI|anthropic\.(Async)?Anthropic" \
    "$SIM" \
    "一律走 ctx.llm()。否则无法计量、无法路由、无法受限（见 docs/design/09-observability.md）。"

check_forbidden \
    "capabilities/ 不直连 LLM 端点" \
    "httpx\.(Async)?Client\(.*base_url|openai\.(Async)?OpenAI|anthropic\.(Async)?Anthropic" \
    "$SRC/capabilities" \
    "一律走 ctx.llm()。供应商适配只允许存在于 llm/ 层。"

# ═════════════════════════════════════════════════════════════
# 结果
# ═════════════════════════════════════════════════════════════
printf '\n%s%s%s\n' "$BOLD" "$(printf '─%.0s' {1..60})" "$RESET"

if [[ $FAILED -eq 0 ]]; then
    printf '%s✓ 架构检查通过%s  共 %s 项，全部符合设计文档\n' \
        "$GREEN$BOLD" "$RESET" "$CHECKS"
    printf '  依据: docs/design/01-architecture.md § 1 依赖方向与红线\n\n'
    exit 0
else
    printf '%s✗ 架构检查失败%s  %s / %s 项违规\n' \
        "$RED$BOLD" "$RESET" "$FAILED" "$CHECKS"
    printf '  依据: docs/design/01-architecture.md § 1 依赖方向与红线\n'
    printf '  如果确实需要偏离设计，请先提交 ADR（%sdocs/adr/%s）并更新设计文档。\n\n' \
        "$CYAN" "$RESET"
    exit 1
fi
