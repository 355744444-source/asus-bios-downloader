# -*- coding: utf-8 -*-
"""
华硕主板 BIOS 下载工具  ASUS Motherboard BIOS Downloader
========================================================
功能：
  1. 主板目录浏览：从华硕官网产品接口（odinapi SeriesFilter / SeriesFilterResult）自动拉取
     全部在售主板（全球站 Intel + AMD 共约 900 款，另补录中国站特供/渠道型号），
     三栏并列：左「芯片组」按「Intel / AMD / 其他」平台归类，中「型号」平铺，右「BIOS 明细」；
     顶部「型号定位 / 搜索」输入关键字可从目录定位，目录无结果时自动打开官网搜索（含已停售型号）。
     目录首次联网加载后写入本地缓存（asus_mb_catalog.json），之后可离线浏览；后台按设置间隔自动刷新。
  2. 选中型号即从华硕 Support 接口获取该主板全部 BIOS 版本列表（官方 JSON API），
     显示 版本 / 发布日期 / 大小 / 更新说明，支持下载任意版本（进度条 / 取消 / SHA-256 校验）。
  3. 数据来源（三类路径自动选择）：
       · 普通在售型号（PRIME / TUF / ProArt / Expedition…）：
         全球站 www.asus.com（ModelMapping 转 hash/pdid 后 GetPDBIOS）；
       · 大陆渠道 / 特供型号（B760M-P 等）：全球站无数据，自动回退中国站 www.asus.com.cn；
       · ROG 型号（ROG STRIX / MAXIMUS / CROSSHAIR…）：走 rog.asus.com 专用接口（RealProductID 直连）。
  4. 设置：目录自动刷新间隔、显示平台 / 芯片组范围可自定义，支持手动「立即更新目录」，
     保存后立即生效，无需重启（设置文件 asus_bios_checker.cfg）。
  5. 华硕版皮肤：ROG 红黑·深色电竞主题。
  6. 目录树列宽自适应（v2.x）：按目录数据实测宽度定 #0 列宽，长型号名不再被裁。
  7. 真·高 DPI（v2.x）：PerMonitorV2 感知 + 全量像素度量按真实 DPI 缩放。
  8. DNS 自愈：运营商/路由器递归 DNS 偶发无法解析官网域名（curl error 6/28）时，
     自动改用 DoH（DNS over HTTPS：阿里 223.5.5.5 / 腾讯 DNSPod，IP 直连不依赖系统 DNS）重试。

技术要点：
  * 华硕官网（www.asus.com / www.asus.com.cn / rog.asus.com）为 Nuxt SSR 站点，
    产品目录走 odinapi.asus.com（SeriesFilter 分类 / SeriesFilterResult 列表），
    BIOS 走 support webapi（GetPDBIOS），均为官网前端同款 JSON 接口。
  * 数据接口：
      SeriesFilter（odinapi，芯片组分类树）
      SeriesFilterResult?Category=<63|64>（odinapi，平台全量型号列表）
      ModelMapping（型号 -> pdid / pdhashedid 映射，普通型号 BIOS 查询前必须先转换）
      www.asus.com/support/webapi/ProductV2/GetPDBIOS（普通型号 BIOS 列表）
      rog.asus.com/support/webapi/product/GetPDBIOS（ROG 型号 BIOS 列表）
  * 官网由 Akamai CDN 防护，普通 urllib/requests 会被 403/拦截，
    故网络层使用 curl_cffi 模拟 Chrome TLS 指纹（impersonate="chrome124"）。
  * BIOS 文件直链 dlcdnets.asus.com（华硕全球下载 CDN，支持断点续传）。

发行：江苏振远智能科技有限公司
"""
import ctypes
import hashlib
import json
import os
import queue
import re
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import webbrowser
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_TITLE = "华硕主板 BIOS 下载工具"
APP_VERSION = "2.3.1"
APP_COMPANY = "江苏振远智能科技有限公司"

# ---------------------------------------------------------------- 常量
UA_CHROME = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
# 华硕全球站（www.asus.com）
BASE = "https://www.asus.com"
# 华硕中国站（www.asus.com.cn）—— 大陆渠道 / 特供型号
CN_BASE = "https://www.asus.com.cn"
# ROG 站（rog.asus.com）—— ROG 系列主板专用
ROG_BASE = "https://rog.asus.com"
# 华硕产品目录 API（odinapi，Nuxt 站点前端同款）
ODIN_API = "https://odinapi.asus.com/recent-data/apiv2"
ODIN_QS = ("SystemCode=asus&WebsiteCode=global"
           "&ProductLevel1Code=motherboards-components"
           "&ProductLevel2Code=motherboards&SeriesWebPath=all-series"
           "&siteID=www&sitelang=")
API_SERIES_FILTER = (ODIN_API + "/SeriesFilter?" + ODIN_QS)          # 分类（平台/芯片组树）
API_SERIES_RESULT = (ODIN_API + "/SeriesFilterResult?" + ODIN_QS)    # 型号列表（Category 过滤）
API_MODEL_MAP = (ODIN_API + "/ModelMapping?SystemCode=asus&{wc}"
                 "&Model1WebPath={m1}&Model2WebPath={m2}&siteID=www&sitelang=")
# 普通型号 BIOS 列表（全球站 / 中国站共用同一 host 路径，website 参数区分）
API_PANEL = (BASE + "/support/webapi/ProductV2/GetPDBIOS?website={host}&model={slug}"
             "&pdhashedid={hashid}&pdid={pdid}&cpu=&siteID=www&sitelang=")
# ROG 型号 BIOS 列表（rog.asus.com 专用 webapi，RealProductID 直连无需 hash）
ROG_API_PANEL = (ROG_BASE + "/support/webapi/product/GetPDBIOS?website=global"
                 "&model={slug}&pdid={pdid}&cpu=&systemCode=rog")
# 下载直链域名（DownloadUrl.Global 为相对路径时拼接）
DL_CDN = "https://dlcdnets.asus.com"
# BIOS 数据源显示名（状态栏 / 日志）：com=全球站 / cn=中国站 / rog=ROG 站
SRC_LABEL = {"com": "华硕全球站 www.asus.com",
             "cn": "华硕中国站 www.asus.com.cn",
             "rog": "ROG 玩家国度站 rog.asus.com"}

IMPERSONATE = "chrome124"
# DNS 自愈（v2.3.0 起，v2.3.1 修补兜底链）：系统 DNS 无法解析官网域名（路由器/运营商递归 DNS 偶发故障，curl error 6/28）时，
# 自动改用 DoH 兜底 —— IP 直连不依赖系统 DNS。国内可直连的两个 DoH 服务，依次尝试。
DOH_URLS = ("https://223.5.5.5/dns-query",    # 阿里公共 DNS（RFC 8484）
            "https://1.12.12.12/dns-query")   # 腾讯 DNSPod（RFC 8484）

# 目录更新失败后的自动重试等待（秒）。v2.3.1
# 3 个解析通道全部试完只要一两秒，而「开机自启 / 睡眠唤醒」时网络栈往往要十几秒才
# 就绪 —— 旧版试完就永久停在失败态，只能等用户自己发现并手动点 ↻。这里按退避自动
# 重试，覆盖住网络就绪窗口；总计约 3 分钟，仍失败则交给用户手动刷新。
CAT_RETRY_DELAYS = (5, 15, 45, 120)

CATALOG_FILE = "asus_mb_catalog.json"     # 本地目录缓存
CFG_FILE = "asus_bios_checker.cfg"        # 用户设置（刷新间隔 / 显示范围）
# ---- 目录树列宽自适应参数（v2.1.1 / v2.0.1 收口）
# 背景：主板型号名长的用 Microsoft YaHei 15px 实测可达 400px+，写死 300px 时右侧被裁。
# v2.0.1：华硕目录实测出「一条离群长名把整栏拉爆」的问题 ——
#   "ROG MAXIMUS XI HERO (WI-FI) Call of Duty - Black Ops 4 Edition" 一条就 466px
#   （150% DPI 下 697px），而型号名第 2 名只有 442px、第 3 名骤降到 331px，
#   90% 以上的型号名都在 300px 以内。按「取最长」定栏宽会让 909 款里 99% 的行
#   右侧空出一大片（左栏被撑到 792px 物理像素 ≈ 528 逻辑像素），观感极差。
# v2.0.1 起：**实测需求仍按最长算，但封顶**，超出部分用省略号 + 悬停完整名称提示
#   （见 _ellipsize / _on_tree_tip）。这样常规型号名一字不少，只有极少数超长名走省略号。
# v2.0.4：缩进收紧。旧值 10 时，芯片组下的主板型号（level 2）文字起点 = 
#   「内容区左内边距 2 + 2×indent + Tk 的展开箭头槽 15」三块叠加 = 37px，
#   而左栏整栏才 360px —— 用户截图反馈「型号名离左边框太远、被截断」。
#   实测确认 Tk 给**每个 item**（含没有箭头的叶子节点）都预留了固定的
#   Treeitem.indicator 槽 15px（DPI 96 / 144 两档均为 15，不随 Tk 缩放）。
# v2.0.5：用户反馈「这种显示还是别截断了」—— 固定 300px 上限仍截断 14 行，
#   其中就包括截图里那条 468px 的限量版名。把**固定上限换成比例上限**：
#     col = clamp(实测需求, CAT_TREE_MIN, min(布局区×45%, 右栏保底))
#   实测全量 977 个节点所需列宽：50% 分位 196px、90% 262px、99% 310px，
#   只有 2 条限量版名需要 468 / 492px。1200 窗口下比例上限 522 不生效、
#   由右栏保底算出的 468 生效 → 截断从 14 行降到 1 行（只剩 Call of Duty 那条）；
#   窗口拉到 1400（比例上限 612 > 492）时 0 行截断。
#   保留比例上限的目的仍是「防离群数据把整栏拉爆」（v2.0.1 的原问题），
#   但不再用一个与窗口无关的固定值 —— 窗口越宽，左栏越能完整显示。
CAT_INDENT = 4         # 左栏芯片组树的每级缩进（设计基准 px），与 Cat.Treeview 的 indent 同源
CAT_COL_MARGIN = 2     # 列宽比实测需求多留的余量（px）：避免「刚好放得下」落在刀刃上
# 文字真实起点 = CAT_TREE_LEAD + depth×indent + CAT_IND_SLOT，三项相加就是
# 「文字离左边框的偏移」。两个新常量都是**固定 px、不随 DPI 缩放**（实测值）。
CAT_TREE_LEAD = 3      # 内容区左内边距（实测 2px）+ 1px 测量余量
CAT_IND_SLOT = 15      # Tk 给每个 item 预留的展开箭头槽宽（叶子节点同样占位）
CAT_BODY_PAD = 38      # 布局区容器自身的左右内边距 + 边框（100% 实测 38px，见 _body_pane_pad）

# ---- 三栏浏览器式布局（v2.2.0）
# 旧版是「左 528 / 右 620」两栏：左栏 910 款型号只能显示 18 行、要滚 50+ 屏；
# 右栏同一时刻却空着 240px。v2.2.0 改为 芯片组 / 型号 / BIOS 明细 三栏 ——
# 层级的「展开」变成列导航：左栏只剩 ~33 行、中栏只剩一个芯片组的型号。
# 宽度预算（1200 窗口：布局区净宽 1136）：
#     225（芯片组） + 340（型号） + 572（BIOS 明细）
# 1100 最小窗口：布局区净宽 1038 → 226+340+490 = 1056 超 18px，
# 按「先借中栏、再借左栏」让位 → 中栏收到 322，右栏正好 490。
PANE_GAP = 12             # 栏间距
TREE_SCROLL_W = 11        # 竖向滚动条宽度（与 Vertical.TScrollbar 的 width 同源）
PICKER_CARD_PAD = 34      # 卡片左右内边距 + 边框（S(16)×2 + S(1)×2）
# 左栏宽度必须容下最长的**平台行**「Intel 主板（554 款）」——
# 实测 _chip_col_need() = 180px（含前缀开销 18 与余量 2），加卡片开销 45 → 225。
# 窄于此平台行的计数会被省略号吃掉，所以 DEFAULT 就定在刚好放下的 226。
PICKER_CHIP_W = 226       # 左栏：芯片组
PICKER_CHIP_W_MIN = 190   # 窗口极窄时的下限（此时平台行会走省略号 + 悬停全名）
# 中栏宽度 340 的依据：华硕目录全量 977 个型号名实测所需宽度
# 50% 分位 196px、90% 分位 261px、99% 分位 310px；340 − 卡片开销 45 − 前缀开销 18 = 277px
# 可用，覆盖到 90% 分位稍上 —— 超长名走省略号 + 悬停全名（用户已确认此取舍）。
PICKER_MODEL_W = 340      # 中栏：型号
PICKER_MODEL_W_MIN = 250
PICKER_RIGHT_MIN = 490    # 右栏（BIOS 明细）保底：不得被前两栏挤到该值以下
CAT_TREE_ROWH = 26        # 芯片组行高（只装芯片组，比旧版 30 紧凑）
MOD_TREE_ROWH = 30        # 型号行高
TV_ROWH = 32              # BIOS 表行高（列表退为导航，36 → 32，把高度让给详情面板）
RIGHT_LIST_MIN = 160      # BIOS 列表保底高（约 4 行）
RIGHT_DETAIL_MIN = 150    # 详情面板保底高（版本行 + 说明 3 行 + SHA-256 + 按钮行）
# 详情面板里「更新说明」最多显示的行数，超出以「…」收尾（悬停看全文）。
# 必须封顶：详情区高度由内容撑出，而 tk.Label 的换行高又取决于它自己的宽度 ——
# 两者互为因果形成**布局反馈环**。实测同一窗口高度下列表区在 291~309px 之间抖动，
# 极端一次详情把列表挤到 1px（BIOS 表 winfo_height()==1）。行数封顶后请求高度
# 有上界，环被切断，布局恢复确定性。
DETAIL_MAX_LINES = 4
# ---- BIOS 列表列宽（v2.0.5）
# 「更新说明」是唯一的可伸缩列，但 Tk 的 stretch **只在有富余时分配**，列宽总和超过
# 控件宽度时不会收缩 → 最后一列被**硬裁**（末尾连省略号都没有，表头也被切）。
# v2.0.5 左栏按需放宽后右栏收到 620px 保底，这个潜伏已久的问题立刻显形
# （见 _layout_tv_columns / _shot_right.png）。改为按实测宽度重排：
#   说明列宽 = 控件宽 − 固定三列基准宽 − 边框占位（下限 TV_NOTE_MIN）
# v2.2.0 收窄：三栏后右栏最窄 498px，卡片内宽 464px —— 固定三列必须一起收，
# 否则 334+140+4 = 478 > 464 又会出现「说明列被硬裁」的老问题（见 _layout_tv_columns）。
TV_COL_BASE = (("ver", 140), ("date", 104), ("size", 80))   # 固定三列基准宽（设计 px）
TV_NOTE_MIN = 120      # 说明列保底宽
TV_COL_OVER = 4        # Treeview 自身边框 / 内边距占位（实测）
# 行高常量（v2.1.0 起集中在常量区）：断言判定的是「渲染出来的行高 == 声明的行高」，
# 实现与判据共用同一个来源，避免断言跟着实现跑。
# v2.2.0：CAT_TREE_ROWH / MOD_TREE_ROWH / TV_ROWH 已在三栏布局常量区定义。
# 右栏型号标题行里「标题」与「打开官网支持页」按钮之间必须留出的空隙（设计 px）。
# 标题按这个值截断，就不会把右侧按钮挤成 0 宽（v2.0.2）。
BOARD_TITLE_GAP = 16
# 目录自动刷新间隔选项：(天, 显示文案)，0 = 不自动刷新
REFRESH_OPTS = ((0, "不自动刷新"), (1, "1 天"), (3, "3 天"),
                (7, "7 天"), (14, "14 天"), (30, "30 天"))
DEFAULT_REFRESH_DAYS = 7
DEFAULT_CFG = {
    "refresh_days": DEFAULT_REFRESH_DAYS,  # 缓存超过 N 天启动时后台自动刷新；0 = 仅手动
    "show_platforms": None,                # None = 全部平台；否则为平台名列表
    "chip_filter": None,                   # None = 全部芯片组；否则为芯片组名列表
    "save_dir": None,                      # v2.0.2：上次用的 BIOS 保存目录（设备可保存）
}
# ---- 文本宽度实测缓存上限（v2.0.2）
# _px_cache 的键是「字体 + 文本」，随目录里的型号名增长。目录全量换一批（4 千多个
# 型号名）就会把它撑到几万条，虽然每次只有几十字节，但**无上界**本身就是隐患。
# 超过上限直接整体丢弃（缓存只是加速，丢了最多重测一次），不做 LRU —— 条目小、
# 重建廉价，LRU 的簿记开销反而更大。
PX_CACHE_MAX = 20000
# ---- 下载大小交叉校验容差（v2.0.2）
# 官网 BIOS 面板的 FileSize 是「13.89 MB」这种两位小数量化值，与实际字节数存在
# <6 KB 的舍入差，不能按等值比较。16 KB 之上就不是舍入而是真截断（CDN 半截镜像）。
DL_SIZE_TOL = 1 << 14

# ================================================================ ROG 红黑·深色电竞主题
# v2.1.0「深色精工」焕新：保留 ROG 红黑血统，把原来的**两级平涂**升级为**三级底面**。
#
#   画布 C_BG  ── 最暗，窗口底色
#     └ 面板 C_CARD ── 卡片，比画布亮一档，靠色差"浮"起来（而非靠粗描边）
#          ├ 抬升条 C_CARD_TOP ── 表头 / 区块头，比面板再亮一点
#          └ 洼地   C_INSET   ── 输入框 / 目录树体 / 表格体，比面板暗一档
#
# 为什么值得分层：旧版卡片与窗底只差 #0d0f12 vs #16181d 且描边 #2b3038 偏亮，
# 视觉上"一片平"；三级底面 + 发丝描边（#252a33）+ 内部发丝线（C_HAIR）能让
# 视线自然分层，不需要加粗边框、也不会显脏。
C_BG = "#0a0c10"           # 画布（窗口底，最暗）
C_CARD = "#12161d"         # 面板（卡片）
C_CARD_TOP = "#171c24"     # 抬升条（表头 / 区块头）
C_INSET = "#0d1015"        # 洼地（输入条 / 目录树体 / 表格体）
C_BORDER = "#252a33"       # 发丝描边（卡片 / 按钮 / 输入框轮廓）
C_HAIR = "#1e232b"         # 内部发丝线（区域分隔 / 状态栏上沿）
C_TEXT = "#eef1f6"         # 主文字（近白）
C_SUB = "#a2aab8"          # 次级文字
C_DIM = "#6f7885"          # 弱化 / 占位 / 状态栏（保持深色下可读）
C_ACCENT = "#ff2d2d"       # ROG 红（品牌条 / 进度条 / 输入框 focus）
C_ACCENT_HOT = "#ff6a4d"   # 红 hover / 高亮（更亮的暖红）
C_ACCENT_DEEP = "#b81e1e"  # 红 pressed
C_ACCENT_BTN = "#e02a24"   # 主操作按钮底（亮红一档，配合黑底更"ROG"）
C_ACCENT_BTN_HOT = "#ff4a3a"   # 主按钮 hover（亮一档）
C_ACCENT_BTN_DEEP = "#a91916"  # 主按钮 pressed
C_ACCENT_BTN_DIS = "#6e2626"   # 主按钮 disabled 底
C_ACCENT_BTN_FG_DIS = "#f2b8b5"  # 主按钮 disabled 字
C_ACCENT_BG = "#2a1618"    # 红弱背景
C_BRAND = C_ACCENT       # 品牌条 / logo 用色（ROG 红）
C_RED_HOT = C_ACCENT_HOT   # 兼容旧引用（小标题强调色）
C_RED_DEEP = C_ACCENT_DEEP # 兼容旧引用
C_RED_BG = "#2a1618"       # 红弱背景（警示/危险容器）
C_SEL_BG = "#341a1d"       # 列表选中背景（ROG 暗红，随新洼地收暗一档）
C_GREEN = "#5fd38a"        # 成功
C_GREEN_BG = "#1b2a1c"     # v2.1.0：随三级底面收暗，与新卡片底协调
C_AMBER = "#f0b429"        # 警告
C_AMBER_BG = "#2b2616"
C_BLUE = "#6cb2f7"         # 信息/链接
C_BLUE_BG = "#18222e"      # v2.1.0：同上
C_STATUS_BG = "#0d1015"    # 状态栏底（与洼地同色，读作"窗底挖下去一条"）
C_STATUS_FG = "#a2aab8"    # 状态栏文字
C_BTN = "#1d222b"          # 次级按钮底
C_BTN_HOVER = "#262c37"    # 次级按钮 hover
C_BTN_PRESS = "#313846"    # 次级按钮 pressed
C_HEADING = C_CARD_TOP     # 表头底（去边框化：靠色阶而非线条分隔）
C_TROUGH = "#0a0d12"       # 进度条槽 / 滚动条槽
C_SCROLL = "#39424f"       # 滚动条滑块（v2.1.0：提亮一档 —— 去掉同色端帽后，
                           #           滑块成为槽里唯一的高亮元素，要看得清）
# 标题栏跟随沉浸式深色：深色皮肤开启，与内容浑然一体
TITLEBAR_DARK = True

# ================================================================ DPI 感知（v2.1.0）
# 设计基准：96 DPI（Windows 100% 缩放）下的像素值。程序启动时**先**声明
# PerMonitorV2 感知、再创建 Tk，然后用 S(px) 把设计像素换算成当前 DPI 的物理像素。
#
# 为什么必须这样做：进程若声明「DPI 不感知」，Windows 会把整个窗口按缩放比
# **位图拉伸**（150% 下每个像素拉成 1.5 个），文字与图标全部发虚；声明感知后
# 由程序自己按物理像素绘制 —— 高缩放比下字形锐利、比例正确。
#
# 为什么字体也用 S()：负值字号是「像素级尺寸」，Tk 的 tk scaling 只作用于
# pt（正值）字号，负值一律按像素解释、不会自动随 DPI 放大，故必须手工换算。
DPI_BASE = 96
DPI_SCALE = 1.0        # 由 _apply_dpi_scale() 设置；100% 缩放时恒为 1.0
_DPI = DPI_BASE        # 探测到的真实 DPI


def S(px):
    """设计像素 → 当前 DPI 的物理像素（四舍五入；非零值至少 1px）。

    100% 缩放下 S(px) == px —— 与旧版渲染逐像素一致。
    """
    if not px:
        return 0
    v = int(round(px * DPI_SCALE))
    if v == 0:
        v = 1 if px > 0 else -1
    return v


def _rebuild_fonts():
    """按当前 DPI_SCALE 重建全部字体元组（负值字号 = 像素，需手工缩放）。

    v2.1.0「深色精工」字号层级重排 —— 旧版存在**层级倒挂**：卡片小标题 20px
    比窗口标题 27px 只小 7px，而正文 15px，导致「主板目录」四个字和窗口标题
    几乎一样抢眼，视线没有落点。新版把级差拉开。

    v2.2.0 三栏重构：标题区从 74px 压到 46px（省下的高度全给正文区），窗口标题
    22 → 19px；主板名标题 21 → 17px —— 右栏从 620px 收到 598px，17px 下超长型号名
    仍留有截断余量（_fit_board_title 按实测像素截断，不靠字号硬扛）。
    层级仍严格递减：标题 19 > 型号 17 > 卡片标题 16 > 正文 15 > 小字 13。

    ⚠️ FONT_S 保持 15px 不动 —— 它是目录树与 BIOS 表格的**测量字体**
    （_fm_tree / _px 系列），改它等于改列宽自适应结果，会牵动列宽整组断言。
    布局重构不该顺带改测量基准，故此处刻意不动。
    """
    global FONT, FONT_B, FONT_ACCENT, FONT_S, FONT_SS, FONT_H, FONT_TITLE, FONT_BOARD, FONT_ICON
    FONT = ("Microsoft YaHei", -S(14))                  # 控件默认字（次级按钮 / 设置窗控件）
    FONT_B = ("Microsoft YaHei", -S(15), "bold")        # 强调（目录树平台节点 / 图标钮）
    FONT_ACCENT = ("Microsoft YaHei", -S(16), "bold")   # 主操作按钮：笔画更粗
    FONT_S = ("Microsoft YaHei", -S(15))                # 正文（**测量字体，勿动**）
    FONT_SS = ("Microsoft YaHei", -S(13))               # 次级小字（表头 / 徽章 / 脚注）
    FONT_H = ("Microsoft YaHei", -S(16), "bold")        # 卡片小标题
    FONT_TITLE = ("Microsoft YaHei", -S(19), "bold")    # 窗口大标题（v2.2.0：22 → 19）
    FONT_BOARD = ("Microsoft YaHei", -S(17), "bold")    # 主板名标题（v2.2.0：21 → 17）
    FONT_ICON = ("Microsoft YaHei", -S(36))             # 空状态图标（v2.2.0：44 → 36）


def _apply_dpi_scale(dpi):
    """设定缩放系数并重建字体（测试可强制指定 DPI 以验证高缩放比布局）。"""
    global _DPI, DPI_SCALE
    _DPI = max(DPI_BASE, int(dpi))
    DPI_SCALE = _DPI / float(DPI_BASE)
    _rebuild_fonts()


def _init_dpi():
    """声明 DPI 感知并探测真实 DPI —— **必须在创建 Tk 之前调用**。

    感知级别依次尝试：PerMonitorV2（Win10 1703+）→ PerMonitor（Win8.1+）→
    System（Vista+）；全部失败则维持系统位图缩放（等同旧版行为，不会更差）。
    """
    aware = False
    try:
        u32 = ctypes.windll.user32
        aware = bool(u32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)))   # PMv2
    except Exception:
        aware = False
    if not aware:
        try:
            aware = ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0        # PerMonitor
        except Exception:
            pass
    if not aware:
        try:
            aware = bool(ctypes.windll.user32.SetProcessDPIAware())            # System
        except Exception:
            pass
    dpi = DPI_BASE
    try:
        dpi = int(ctypes.windll.user32.GetDpiForSystem())
    except Exception:
        try:
            hdc = ctypes.windll.user32.GetDC(0)
            dpi = int(ctypes.windll.gdi32.GetDeviceCaps(hdc, 88))   # LOGPIXELSX
            ctypes.windll.user32.ReleaseDC(0, hdc)
        except Exception:
            dpi = DPI_BASE
    _apply_dpi_scale(dpi)
    return aware


def _work_area():
    """主显示器工作区尺寸（物理像素；感知进程下即真实分辨率）。"""
    class _RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
    r = _RECT()
    try:
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
            if r.right > r.left and r.bottom > r.top:
                return (r.right - r.left, r.bottom - r.top)
    except Exception:
        pass
    try:
        return (ctypes.windll.user32.GetSystemMetrics(0),
                ctypes.windll.user32.GetSystemMetrics(1))
    except Exception:
        return (1280, 720)


def _fit_size(w, h, margin=0):
    """设计尺寸 → 物理像素，并夹紧到屏幕工作区内。

    高缩放比 + 小屏（如 1920×1080 @150%）时 S(1200)=1800 已接近满屏，
    不夹紧会让窗口（含最小尺寸）超出屏幕导致部分内容永远点不到。
    """
    aw, ah = _work_area()
    m = S(margin)
    return (min(S(w), max(320, aw - m)), min(S(h), max(240, ah - m)))


def _ver_suffix():
    """状态栏版本号后缀：非 100% 缩放时附 DPI，便于确认感知已生效。"""
    if abs(DPI_SCALE - 1.0) < 0.001:
        return "v%s" % APP_VERSION
    return "v%s · DPI %d%%" % (APP_VERSION, int(round(DPI_SCALE * 100)))


# 初始（96 DPI）字体 —— 启动时 _init_dpi() 会按真实 DPI 重建
_rebuild_fonts()

# ---------------------------------------------------------------- 动效（v2.4.0）
# 统一帧动画：所有动画共用一个 after 循环，动画清空即停表 —— 空闲时零 CPU 占用
ANIM_FPS_MS = 16          # 帧间隔（~60fps）
ANIM_FADE = 200           # 列表淡入
ANIM_PB = 300             # 进度条补间
ANIM_HOVER = 140          # 按钮 / 链接 hover 渐变
ANIM_FLASH = 460          # 完成脉冲
ANIM_SHAKE = 280          # 搜索无结果抖动
SEARCH_PH = "搜索型号 / 芯片组…"   # 搜索框占位文案

# v2.1.0：行 hover 底改为相对**新洼地**（C_INSET）提亮一档 —— 表格体已沉入洼地，
# 原来的 #2c313d 是按卡片底调的，现在会跳得过高、像贴了一条胶带。
C_ROW_HOVER = "#1a1f28"   # 列表行 hover 底（不与选中暗红抢焦点）
C_LATEST_HOVER = "#20301f"  # 「最新」行 hover 底（绿底提亮一档）
C_SKELETON = "#232933"    # 骨架 / 占位行
C_OK = "#2f9e5f"          # 完成态（进度条红→绿脉冲目标）
C_OK_BG = "#182420"
C_OK_BORDER = "#2c6440"


def _ease_out(p):
    """三次缓出：起步快、收尾稳 —— 界面动效的默认曲线。"""
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    return 1.0 - (1.0 - p) ** 3


def _lerp_color(c1, c2, p):
    """两色线性插值（#rrggbb）。p<=0 返回 c1，p>=1 返回 c2。"""
    if p <= 0.0:
        return c1
    if p >= 1.0:
        return c2
    try:
        a = (int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16))
        b = (int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16))
        return "#%02x%02x%02x" % tuple(
            max(0, min(255, int(round(a[i] + (b[i] - a[i]) * p)))) for i in range(3))
    except Exception:
        return c2 if p >= 0.5 else c1


def _fmt_bytes(n):
    """人类可读体积：B / KB / MB / GB。"""
    try:
        n = float(n)
    except Exception:
        return "—"
    for unit, div in (("GB", 1073741824.0), ("MB", 1048576.0), ("KB", 1024.0)):
        if n >= div:
            return "%.1f %s" % (n / div, unit)
    return "%d B" % int(n)


def _fmt_eta(sec):
    """剩余时间：mm:ss / h:mm:ss。"""
    try:
        sec = int(max(0, sec))
    except Exception:
        return "—"
    if sec >= 3600:
        return "%d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)
    return "%d:%02d" % (sec // 60, sec % 60)


DL_CHUNK = 64 * 1024


# ---------------------------------------------------------------- 日志/路径
def _app_dir():
    """程序目录：打包后为 exe 所在目录；源码运行时为**脚本所在目录**。

    v2.0.2：源码态原先取 os.getcwd() —— 从别的目录启动（快捷方式、计划任务、
    拖到 cmd 里跑）时，配置 / 缓存 / 日志会落到那个目录，换个目录启动就
    「设置丢了」、旧缓存也读不到。改为锚定脚本自身，与打包态语义一致。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


# 目录可写性探测结果：{规范化目录: bool}
_writable_cache = {}


def _dir_writable(d):
    """目录**真的**可写吗 —— 实测落一个探针文件，不信 os.access。

    v2.0.2：Windows 上 os.access(d, os.W_OK) 只看「只读属性位」，**不查 ACL**：
    对 icacls 拒绝写入的目录照样返回 True。用它做守卫的结果是 save_cfg 抛
    PermissionError(13)，而界面照样提示「设置已保存」—— 用户以为存了，其实没存。
    这里改为真写一次（名字带 pid，避免并发探测互踩），结果按目录缓存，
    正常路径下每次落盘不会多出 IO。
    """
    key = os.path.normcase(os.path.abspath(d))
    hit = _writable_cache.get(key)
    if hit is not None:
        return hit
    probe = os.path.join(d, "._wrtest_%d" % os.getpid())
    ok = False
    try:
        with open(probe, "wb") as f:
            f.write(b"1")
        ok = True
    except Exception:
        ok = False
    finally:
        try:
            os.remove(probe)
        except Exception:
            pass
    _writable_cache[key] = ok
    return ok


def _data_path(name):
    """数据文件路径（配置 / 缓存 / 日志共用一套回退规则）。

    先程序目录（用户可见、可随程序一起搬走）；程序目录不可写时退回临时目录，
    保证「能存住」这一功能不因安装位置而失效。若程序目录里已有可读文件而目录
    不可写，则仍然就着它读（不丢历史数据）。

    v2.0.0：原 _cache_path / _cfg_path 两段完全同构，合并到这里。
    v2.0.2：可写判断由 os.access 改为 _dir_writable 实测（见上）。
    """
    d = _app_dir()
    if os.path.isdir(d) and _dir_writable(d):
        return os.path.join(d, name)
    legacy = os.path.join(d, name)
    if os.path.isfile(legacy):
        return legacy
    return os.path.join(tempfile.gettempdir(), name)


def _tmp_path(final):
    """原子写盘的临时名：与目标同目录，且**按写者唯一**。

    v2.0.2：原先是固定的 `目标 + ".tmp"`，两个线程（例如目录刷新与设置保存）
    同时写同一个文件时共用一个临时路径 —— 后写者的 open(...,"w") 会截断前写者
    正在写的内容，os.replace 的「原子」只保证换名、不保证内容是你的。带上
    pid + tid 后每个写者各写各的临时文件，replace 仍然原子。
    """
    return "%s.%d.%d.tmp" % (final, os.getpid(), threading.get_ident())


def _atomic_write_text(path, text):
    """写文本文件：唯一临时名 → flush + fsync → os.replace。

    fsync 是 v2.0.2 补的：os.replace 只保证「换名」原子，不保证内容已经落到
    盘上 —— 断电/蓝屏时可能留下「新名字 + 空/半截内容」。配置与缓存都不大，
    同步一次的成本远低于丢设置。
    """
    tmp = _tmp_path(path)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise


def _cache_path():
    """目录缓存文件路径。"""
    return _data_path(CATALOG_FILE)


def _cfg_path():
    """用户设置文件路径。"""
    return _data_path(CFG_FILE)


def load_cfg():
    """读取用户设置；文件缺失/损坏/缺键/类型异常时回退默认值。"""
    cfg = dict(DEFAULT_CFG)
    try:
        p = _cfg_path()
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                # v2.3.5 类型校验：手改损坏/旧版文件不致命
                if "refresh_days" in data:
                    try:
                        cfg["refresh_days"] = int(data["refresh_days"])
                    except (TypeError, ValueError):
                        cfg["refresh_days"] = DEFAULT_REFRESH_DAYS
                for k in ("show_platforms", "chip_filter"):
                    v = data.get(k)
                    cfg[k] = v if isinstance(v, list) else None
                # v2.0.2：上次使用的保存目录
                sd = data.get("save_dir")
                cfg["save_dir"] = sd if isinstance(sd, str) and sd else None
    except Exception as e:
        _log("load_cfg err: %r" % (e,))
    return cfg


def save_cfg(cfg):
    try:
        _atomic_write_text(_cfg_path(), json.dumps(cfg, ensure_ascii=False))
        return True
    except Exception as e:
        _log("save_cfg err: %r" % (e,))
        return False


_LOG_FILE = "asus_bios_checker.log"
_LOG_MAX = 1 << 20          # 1 MB：超过即轮转一次，避免长期运行把日志撑爆


def _log(msg):
    """追加一行日志；文件超过 _LOG_MAX 时先轮转为 .1（只保留一代）。"""
    try:
        p = _data_path(_LOG_FILE)
        try:
            if os.path.isfile(p) and os.path.getsize(p) > _LOG_MAX:
                os.replace(p, p + ".1")
        except Exception:
            pass
        with open(p, "a", encoding="utf-8") as f:
            f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + msg + "\n")
    except Exception:
        pass


def _is_dns_error(e):
    """判断异常是否为 DNS 解析失败（curl error 6 Could not resolve / 28 Resolving timed out / DNSError）。"""
    try:
        m = str(e).lower()
    except Exception:
        return False
    return ("could not resolve" in m or "resolving timed out" in m
            or "dns" in type(e).__name__.lower() or "esolv" in m)


def _net_err_text(e):
    """网络异常 → 一句人能读的话（状态栏用）。完整异常由调用方写日志。"""
    s = str(e or e.__class__.__name__)
    low = s.lower()
    if _is_dns_error(e):
        return "域名解析失败（本机 DNS / 路由器无响应）"
    if "timed out" in low or "timeout" in low:
        return "连接超时（网络较慢或被拦截）"
    if "ssl" in low or "tls" in low or "certificate" in low:
        return "TLS 握手失败（可能被网络设备干扰）"
    if "refused" in low:
        return "连接被拒绝（目标端口不通）"
    if "reset" in low or "closed" in low:
        return "连接被中断"
    if "proxy" in low:
        return "代理不可用"
    if low.startswith("http ") or "http " in low[:8]:
        return "请求失败（%s）" % s.strip()
    return s[:60]


def _dns_fallback(attempt, retry=2, tag="网络"):
    """统一解析兜底：系统 DNS 重试 → 各 DoH 服务器，直到成功或试完。

    attempt(doh_url) 执行一次请求，失败抛异常；doh_url 为 None 表示走系统 DNS。

    v2.3.1：修复旧实现的两个洞 ——
      ① 旧版一旦某个 DoH 抛非解析类异常（SSL 握手失败 / 连接被重置，国内对
         223.5.5.5:443 做干扰时很常见）就立刻 raise，**后面的 DoH 永远走不到**；
      ② 旧版 DNS 失败后仍把剩余 retry 次数耗在系统 DNS 上，白等一个连接超时。
    现在：只要出现过解析类失败，就进入「通道全试完」模式，中途任何异常都只记录不改道。
    """
    last = None
    dns_seen = False
    tried = []
    # 阶段一：系统 DNS（快，且能吃到系统缓存）。非解析类故障就地重试，不进 DoH。
    for _ in range(max(1, retry)):
        tried.append("系统DNS")
        try:
            return attempt(None)
        except Exception as e:
            last = e
            dns_seen = dns_seen or _is_dns_error(e)
            if dns_seen:
                break                 # 已判定解析故障：剩余重试只会白等超时，直接换通道
            time.sleep(0.5)
    if not dns_seen:
        raise RuntimeError(_net_err_text(last)) from None
    # 阶段二：逐个 DoH（IP 直连，不依赖系统 DNS），试完为止。
    for doh in DOH_URLS:
        tried.append(doh)
        try:
            return attempt(doh)
        except Exception as e:
            last = e
            time.sleep(0.4)
    _log("%s：解析通道全部失败 %s" % (tag, tried))
    raise RuntimeError(
        "域名解析失败，已依次尝试 %d 个备用解析通道（DoH）仍未成功"
        % len(DOH_URLS)) from None


def _cr_get_dns_safe(url, **kw):
    """带解析兜底的 GET（下载直链用）。

    用法与 curl_cffi requests.get 一致（headers / stream / timeout / impersonate 原样透传）。
    """
    from curl_cffi import requests as cr

    def attempt(doh):
        if doh is None:
            return cr.get(url, **kw)
        return cr.get(url, doh_url=doh, **kw)

    return _dns_fallback(attempt, tag="下载直链")


def clean_version(v):
    """展示用版本字符串（仅清洗空白）。"""
    if not v:
        return "—"
    return re.sub(r"\s+", "", str(v))


def slugify(name):
    """主板名 -> 官网 URL slug。'MPG Z490 GAMING EDGE WIFI (MS-7C79)' -> MPG-Z490-GAMING-EDGE-WIFI"""
    s = re.sub(r'[\(\（].*?[\)\）]', '', str(name or ""))
    s = re.sub(r'[^A-Za-z0-9]+', '-', s.upper())
    return s.strip('-')


_JUNK_TITLE = {"XX", "XXX", "TEST"}
def _is_junk_board(board):
    """过滤官网列表中的占位/配件条目（如 xx、TPM 2.0 Module、已移除占位）。"""
    title = (board.get("title") or "").strip().upper()
    if not title or title in _JUNK_TITLE or len(title) < 2:
        return True
    if re.search(r'\b(TPM|MODULE|ACCESSORY)\b', title):
        return True
    if "(REMOVE" in title or title.startswith("("):
        return True
    return False


# ================================================================ ASUS API 客户端
def _strip_h(html):
    """去掉 Name 字段里的 <h2>…</h2> 等 HTML 标签，取纯文本型号名。"""
    return re.sub(r"<[^>]+>", "", html or "").strip()


def _to_bytes(size_txt):
    """把官网的 '13.89 MB' / '850 KB' / '1.2 GB' 转成字节数（int）；解析不了返回 0。"""
    if size_txt is None:
        return 0
    m = re.search(r"([\d.]+)\s*(B|KB|MB|GB)", str(size_txt).strip(), re.I)
    if not m:
        try:
            return int(str(size_txt).strip())
        except Exception:
            return 0
    val = float(m.group(1))
    unit = m.group(2).upper()
    return int(val * {"B": 1, "KB": 1024, "MB": 1048576, "GB": 1073741824}[unit])


class AsusApi:
    def __init__(self):
        from curl_cffi import requests as cr
        self._cr = cr
        self._sess = cr.Session(impersonate=IMPERSONATE, timeout=(15, 60))
        # curl_cffi Session 非线程安全；所有请求加锁串行化
        self._lock = threading.Lock()
        # 缓存 SeriesFilter 芯片组分类（一次拉取全程序复用）
        self._chipset_tree = None

    def _get_json(self, url, retry=2):
        """GET JSON；带解析兜底：系统 DNS 失败时自动改用 DoH 重试（见 _dns_fallback）。

        v2.3.1：错误文案改为一句人话 —— 旧版把 curl 原文和 libcurl 帮助链接直接糊到状态栏。
        """
        # referer / 语言：按目标域名区分（asus.com / asus.com.cn / rog.asus.com / odinapi）
        if "asus.com.cn" in url:
            ref, lang = CN_BASE + "/", "zh-CN,zh;q=0.9"
        elif "odinapi" in url:
            ref, lang = BASE + "/motherboards-components/motherboards/all-series/", "zh-CN,zh;q=0.9"
        else:
            ref, lang = BASE + "/", "en-US,en;q=0.9"
        hdrs = {"User-Agent": UA_CHROME,
                "Accept": "application/json,text/plain,*/*",
                "Accept-Language": lang,
                "Referer": ref}

        def attempt(doh):
            kw = {"headers": hdrs}
            if doh is not None:
                kw["doh_url"] = doh
            with self._lock:            # curl_cffi Session 非线程安全，串行化
                r = self._sess.get(url, **kw)
            if r.status_code != 200:
                raise RuntimeError("HTTP %s" % r.status_code)
            return r.json()

        return _dns_fallback(attempt, retry, tag=url)

    # ---------------- 芯片组分类树（SeriesFilter，一次拉取缓存）
    def get_chipset_codes(self):
        """从 SeriesFilter 提取芯片组码树，返回 [{'plat':'Intel'|'AMD','code':'Z890'}, …]
        注意华硕官网把 X870E/X870 等归同一过滤组，code 即官网过滤粒度。"""
        if self._chipset_tree is not None:
            return self._chipset_tree
        j = self._get_json(API_SERIES_FILTER)
        out = []
        for sc in (j.get("Result") or {}).get("SpecCategory") or []:
            if sc.get("Name") != "Chipset":
                continue
            for grp in sc.get("Item") or []:
                plat = grp.get("Name")
                if plat not in ("Intel", "AMD"):
                    continue
                for it in grp.get("SubItem") or []:
                    code = re.sub(r"^(Intel|AMD)\s+", "", it.get("Name") or "", flags=re.I)
                    code = re.sub(r"\s+", "", code).upper()
                    if code:
                        out.append({"plat": plat, "code": code})
        # 长码优先：标题匹配时先命中 X299 再 X99，避免短码误吞
        out.sort(key=lambda x: -len(x["code"]))
        self._chipset_tree = out
        return out

    # ---------------- 平台全量型号列表（SeriesFilterResult, Category=63 Intel / 64 AMD）
    def get_platform_products(self, cat_id):
        """拉取一个平台（Category）的**全量**在售型号列表（单次返回全部，无分页）。
        返回 [{'id','title','slug','url','src','hashid','real_pid','webpath'}]"""
        url = API_SERIES_RESULT + "&Category=%s" % cat_id
        j = self._get_json(url)
        res = j.get("Result") or {}
        items = []
        for p in res.get("ProductList") or []:
            title = _strip_h(p.get("Name"))
            url_p = (p.get("ProductURL") or "").strip()
            slug = url_p.rstrip("/").split("/")[-1] if url_p else slugify(title)
            if not title or not slug:
                continue
            items.append({
                "id": p.get("ProductID") or slug,
                "title": title,
                "slug": slug,
                "url": url_p,
                "src": "rog" if (p.get("SourceType") == "rog") else "prod",
                "hashid": (p.get("ProductHashedID") or "").strip(),
                "real_pid": p.get("RealProductID") or 0,
                "webpath": (p.get("PDWebPath") or "").strip(),
            })
        return items

    # ---------------- 型号 -> pdid / pdhashedid（ModelMapping，普通型号 BIOS 前必须转换）
    def model_mapping(self, wc, m1, m2):
        """ModelMapping 转换：m1=小写 slug，m2=大写 WebPath（PDWebPath）。
        返回 {'hash': HashedID, 'pdid': Value} 或 None（该站无此型号）。"""
        url = API_MODEL_MAP.format(wc="WebsiteCode=%s" % wc, m1=m1, m2=m2)
        try:
            j = self._get_json(url)
        except RuntimeError:
            return None
        lst = ((j.get("Result") or {}).get("List")) or []
        if not lst:
            return None
        it = lst[0]
        h = (it.get("HashedID") or "").strip()
        v = it.get("Value") or 0
        return {"hash": h, "pdid": v} if h and v else None

    # ---------------- BIOS 明细（华硕分三类路径自动选择）
    def get_bios_panel(self, board, dtype="bios"):
        """查询某主板的 BIOS 列表。board 为目录条目 dict：
           {slug, title, src:'prod'|'rog', hashid, real_pid, webpath, url}
        路径（普通型号双站回退，ROG 型号走专用接口）：
          1) prod 普通型号：先全球站（ModelMapping->GetPDBIOS website=global）；
             无数据（大陆渠道/特供版）自动回退中国站（website=cn）。
          2) rog 型号：走 rog.asus.com 专用 webapi（RealProductID 直连，无需 hash）。
        返回 (result_or_None, 'com'|'cn'|'rog')。"""
        if board.get("src") == "rog":
            try:
                url = ROG_API_PANEL.format(slug=board["slug"], pdid=board.get("real_pid") or 0)
                j = self._get_json(url)
                if j.get("Status") == "SUCCESS":
                    return j.get("Result"), "rog"
                return None, "rog"
            except RuntimeError:
                return None, "rog"

        # prod：全球站优先，无数据回退中国站
        slug = board["slug"]
        m2 = board.get("webpath") or slug.upper()
        last_res, last_host = None, "com"
        for host, wc in (("com", "global"), ("cn", "cn")):
            mm = self.model_mapping(wc, slug, m2)
            if not mm:
                continue
            url = API_PANEL.format(host=("global" if host == "com" else "cn"),
                                   slug=slug, hashid=mm["hash"], pdid=mm["pdid"])
            try:
                j = self._get_json(url)
            except RuntimeError:
                if host == "com":
                    continue
                break
            res = j.get("Result")
            if isinstance(res, dict) and res.get("Obj"):
                return res, host
            last_res, last_host = res, host
        return last_res, last_host

    def parse_bios_groups(self, panel):
        """从 GetPDBIOS Result 提取 BIOS 分组：{badge: [归一化 item...]}。
        华硕响应结构：Result.Obj[] = [{Name:'BIOS', Files:[{Version,ReleaseDate,FileSize,
        Description,DownloadUrl:{Global,China},sha256,...}]}]
        归一化字段与 GUI 其余部分一致，可直接消费：
          download_version / download_release / download_size(字节) /
          download_description / download_url / download_sha256。
        返回 (groups, os_list, note, title)。"""
        groups = {}
        objs = panel.get("Obj") if isinstance(panel, dict) else None
        if not objs:
            return {}, None, "", ""
        title = panel.get("Model") or ""
        for o in objs:
            name = (o.get("Name") or "").strip() or "BIOS"
            # 主 BIOS 组名统一为 'BIOS'（含 Intel ME 等固件另列分组，UI 多组下拉可切换）
            badge = name
            files = []
            for f in o.get("Files") or []:
                dl = f.get("DownloadUrl") or {}
                raw = dl.get("Global") or dl.get("China")
                if not raw:
                    continue
                if raw.startswith("http"):
                    url = raw
                else:
                    url = DL_CDN + raw
                desc = (f.get("Description") or "").replace('"', "")
                files.append({
                    "download_version": str(f.get("Version") or "").strip(),
                    "download_release": (f.get("ReleaseDate") or "").strip(),
                    "download_size": _to_bytes(f.get("FileSize")),
                    "download_description": desc,
                    "download_url": url,
                    "download_sha256": (f.get("sha256") or "").strip(),
                })
            if files:
                groups.setdefault(badge, []).extend(files)
        return groups, None, "", title


# ================================================================ 目录构建
def fetch_catalog(api, progress=None):
    """从华硕官网构建完整主板目录。
    返回: {'updated': str, 'total': int,
           'brands': [{'name':'Intel','count':n,'chipsets':[
                        {'name':'Z890','boards':[{id,title,slug,url,src,...}], ...}], ...}, ...]}
    目录条目 board 额外携带 url/src/hashid/real_pid/webpath（BIOS 查询分流用）。
    """
    codes = api.get_chipset_codes()
    if not codes:
        raise RuntimeError("官网芯片组分类为空")

    # 平台常量：官网 Category=63 -> Intel，64 -> AMD
    CAT_PLAT = ((63, "Intel"), (64, "AMD"))
    brands = {}          # name -> {'count':0, 'chipsets': [ {name, boards} ] }

    def _add_board(brand, chip, board):
        b = brands.setdefault(brand, {"count": 0, "chipsets": []})
        cs = next((c for c in b["chipsets"] if c["name"] == chip), None)
        if cs is None:
            cs = {"name": chip, "boards": []}
            b["chipsets"].append(cs)
        cs["boards"].append(board)
        b["count"] += 1

    def _classify(title):
        """标题 -> (brand, chip)。用芯片组码做子串匹配（长码优先），
        匹配不到落 (None, None) → 归入「其他」。标题去空格防 B760M 等误伤码边界。"""
        t = re.sub(r"\s+", "", title.upper())
        for cd in codes:
            # 含码即命中。B760M-A 含 B760；X870E 归 X870 组（官网同粒度）
            if cd["code"] in t:
                return cd["plat"], cd["code"]
        return None, None

    full_clean = []
    for cat_id, plat in CAT_PLAT:
        if progress:
            progress("正在拉取 %s 全量型号列表…" % plat)
        try:
            items = api.get_platform_products(cat_id)
        except Exception as e:
            _log("platform fetch err %s: %r" % (plat, e))
            items = []
        seen = set()
        for it in items:
            if it["id"] in seen:
                continue
            seen.add(it["id"])
            if _is_junk_board(it):
                continue
            full_clean.append(it)
            brand, chip = _classify(it["title"])
            _add_board(brand or "其他", chip or "其他 / 未分类", it)

    # 组装（Intel / AMD 顺序固定，其他最后）
    out_brands = []
    for name in ("Intel", "AMD", "其他"):
        if name in brands:
            b = brands[name]
            for cs in b["chipsets"]:
                # 去重保序
                seen = set()
                kept = []
                for x in cs["boards"]:
                    if x["id"] in seen:
                        continue
                    seen.add(x["id"])
                    kept.append(x)
                cs["boards"] = kept
            # 同平台芯片组保持官网码树顺序（按 codes 出现序）
            order = {cd["code"]: i for i, cd in enumerate(codes)} if name != "其他" else {}
            b["chipsets"].sort(key=lambda c: order.get(c["name"], 10 ** 9))
            out_brands.append({"name": name, "count": b["count"],
                               "chipsets": b["chipsets"]})
    cat = {"updated": time.strftime("%Y-%m-%d %H:%M"),
           "total": len(full_clean), "brands": out_brands, "uncovered": []}
    # 中国站补全：把 asus.com.cn 在售但全球站缺失的型号（大陆渠道/特供等）补录进来
    merge_cn_products(cat, api)
    return cat


def merge_cn_products(cat, api=None):
    """华硕中国站(www.asus.com.cn)目录补全。

    拉取中国大陆全量在售主板列表，与 cat 现有型号比对：
      * 已记录（slug / title 归一化命中）→ 滤过
      * 未记录且非占位 → 按标题中的芯片组码归入对应平台/芯片组并补录
        （如 B760M-P、B760M-AYW WIFI 淮洋易通定制版、B460M-P 等大陆渠道板）
      * 归属不到芯片组 → 收进「其他 / 未分类」

    就地修改 cat（在 fetch_catalog 流程里被调用，也支持对旧缓存文件复用）。
    返回补录的 [(title, slug, "平台/芯片组"|"其他")]；网络失败返回 None（调用方忽略）。
    """
    if api is None:
        api = api_ref()
    try:
        codes = api.get_chipset_codes()
    except Exception:
        codes = None
    if codes is None:
        return None

    all_items = []
    for cat_id in (63, 64):
        try:
            u = (API_SERIES_RESULT + "&Category=%s" % cat_id).replace(
                "WebsiteCode=global", "WebsiteCode=cn")
            r = _cr_get_dns_safe(u, impersonate=IMPERSONATE, timeout=25,
                                 headers={"User-Agent": UA_CHROME,
                                          "Accept": "application/json,text/plain,*/*",
                                          "Accept-Language": "zh-CN,zh;q=0.9",
                                          "Referer": CN_BASE + "/"})
            try:
                j = r.json()
            finally:
                r.close()
            for p in (j.get("Result") or {}).get("ProductList") or []:
                title = _strip_h(p.get("Name"))
                url_p = (p.get("ProductURL") or "").strip()
                slug = url_p.rstrip("/").split("/")[-1] if url_p else slugify(title)
                if title and slug:
                    all_items.append({
                        "id": p.get("ProductID") or slug, "title": title, "slug": slug,
                        "url": url_p,
                        "src": "rog" if p.get("SourceType") == "rog" else "prod",
                        "hashid": (p.get("ProductHashedID") or "").strip(),
                        "real_pid": p.get("RealProductID") or 0,
                        "webpath": (p.get("PDWebPath") or "").strip(),
                        "host": "cn",
                    })
        except Exception as e:
            _log("cn merge fetch err: %r" % (e,))
            return None

    def norm(s):
        return re.sub(r"[^a-z0-9]", "", (s or "").lower())

    # 1) 现有型号登记
    known = set()
    for b in cat.get("brands") or []:
        for cs in b.get("chipsets") or []:
            for bd in cs.get("boards") or []:
                known.add(norm(bd.get("slug", "")))
                known.add(norm(bd.get("title", "")))

    def _find_brand(name):
        for b in cat.get("brands") or []:
            if b.get("name") == name:
                return b
        nb = {"name": name, "count": 0, "chipsets": []}
        cat.setdefault("brands", []).append(nb)
        return nb

    added = []
    for it in all_items:
        title, slug = it["title"], it["slug"]
        if not title or _is_junk_board(it):
            continue
        if norm(slug) in known or norm(title) in known:
            continue
        # 归属
        brand = chip = None
        t2 = re.sub(r"\s+", "", title.upper())
        for cd in codes:
            if cd["code"] in t2:
                brand, chip = cd["plat"], cd["code"]
                break
        b = _find_brand(brand or "其他")
        csname = chip if brand else "其他 / 未分类"
        cs = next((c for c in b["chipsets"] if c["name"] == csname), None)
        if cs is None:
            cs = {"name": csname, "boards": []}
            b["chipsets"].append(cs)
        cs["boards"].append(it)
        b["count"] += 1
        added.append((title, slug, ("%s / %s" % (brand, chip)) if brand else "其他"))
        known.add(norm(slug))
        known.add(norm(title))
    if added:
        # 重算总款数（保证 total 与实际补录一致）
        total = sum(len(cs.get("boards") or [])
                    for b in cat.get("brands") or []
                    for cs in b.get("chipsets") or [])
        cat["total"] = total
    return added


_api_singleton = None


def api_ref():
    """供 merge_cn_products 复用的全局 API 单例（避免重复建 Session）。"""
    global _api_singleton
    if _api_singleton is None:
        _api_singleton = AsusApi()
    return _api_singleton


def save_catalog(cat):
    try:
        _atomic_write_text(_cache_path(), json.dumps(cat, ensure_ascii=False))
        return True
    except Exception as e:
        _log("save_catalog err: %r" % (e,))
        return False


def load_catalog_cached():
    """读取本地缓存目录；不存在返回 None。返回 (catalog, age_seconds)"""
    try:
        p = _cache_path()
        if not os.path.isfile(p):
            return None, -1
        with open(p, "r", encoding="utf-8") as f:
            cat = json.load(f)
        age = time.time() - os.path.getmtime(p)
        return (cat if cat.get("brands") else None), age
    except Exception as e:
        _log("load_catalog err: %r" % (e,))
        return None, -1


# ================================================================ 下载
def do_download(url, dest, progress_cb=None, cancel_event=None, expect_size=0):
    """流式下载到 dest.part，完成后改名；返回 (ok, path, err, sha256_local)

    注：curl_cffi 的 Response 不支持 `with` 上下文协议（区别于 requests），故手动 close。
    v2.3.0：DNS 解析失败时自动走 DoH 兜底重试（dlcdnets.asus.com 华硕 CDN，可能解析故障）。
    v2.0.2：新增 expect_size 交叉校验 —— HTTP 层面「Content-Length 与实际字节数一致」
    只能证明这次传输完整，不能证明拿到的就是官网那版 BIOS（CDN 半截镜像 / 中间缓存
    返回旧文件都满足前者）。官网面板的 FileSize 是独立来源，两者对不上就丢弃文件，
    避免把不完整的 BIOS 交给用户刷主板。容差见 DL_SIZE_TOL。
    """
    tmp = dest + ".part"
    headers = {"User-Agent": UA_CHROME}
    r = None
    try:
        r = _cr_get_dns_safe(url, impersonate=IMPERSONATE, headers=headers,
                             stream=True, timeout=(20, 60))
        if r.status_code not in (200, 206):
            return False, None, "服务器返回 HTTP %s" % r.status_code, None
        total = 0
        try:
            total = int(r.headers.get("Content-Length") or 0)
        except Exception:
            total = 0
        done = 0
        last = time.time()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(DL_CHUNK):
                if cancel_event is not None and cancel_event.is_set():
                    f.close()
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass
                    return False, None, "已取消", None
                f.write(chunk)
                done += len(chunk)
                now = time.time()
                if progress_cb and (now - last >= 0.08 or done >= total):
                    last = now
                    if not progress_cb(done, total):
                        if cancel_event is not None:
                            cancel_event.set()
                        # QA 复审修复：此分支原先直接 return，没删半截 .part（实测残留
                        # 1 MB）。与上面 cancel_event 分支保持一致：先释放句柄再删。
                        f.close()
                        try:
                            os.remove(tmp)
                        except Exception:
                            pass
                        return False, None, "已取消", None
        # v2.0.2：与官网标注大小交叉校验（下载完整 ≠ 文件对版）
        if expect_size > 0:
            tol = max(DL_SIZE_TOL, expect_size // 1000)
            if abs(done - expect_size) > tol:
                try:
                    os.remove(tmp)
                except Exception:
                    pass
                return False, None, ("文件大小与官网标注不符（官网 %.2f MB / 实收 %.2f MB），"
                                     "已丢弃，请重试或改用官网直链" %
                                     (expect_size / 1048576.0, done / 1048576.0)), None
        os.replace(tmp, dest)
        return True, dest, None, None
    except Exception as e:
        try:
            os.remove(tmp)
        except Exception:
            pass
        return False, None, repr(e)[:200], None
    finally:
        try:
            if r is not None:
                r.close()
        except Exception:
            pass


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def parse_sha256_from_field(s):
    """'SHA-256:ea5f...<br>' -> hex 或 None"""
    if not s:
        return None
    m = re.search(r'[0-9a-fA-F]{64}', s)
    return m.group(0).lower() if m else None


def _safe_filename(url, ver=""):
    """从下载直链推导安全文件名（v2.0.0）。

    去掉查询串与路径分隔符，过滤 Windows 非法字符并剥掉首尾的「.」/空格，
    防止 `..` 或绝对路径让文件落到用户选定目录之外。
    """
    path = urllib.parse.urlparse(str(url or "")).path
    name = os.path.basename(path.rstrip("/").split("/")[-1])
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if not name:
        v = ver if ver and ver != "—" else "download"
        name = "bios_%s.zip" % v
    if not re.search(r'\.(zip|rar|7z|exe)$', name, re.I):
        name += ".zip"
    return name


# ================================================================ 帧动画调度器
class Anim:
    """统一帧动画调度器（v2.4.0）。

    所有动画注册到**同一个** after 循环：多个动画并行时只跑一个定时器，
    动画清空后自动停表 —— 空闲时零 CPU 占用。
    （若每个动画各起一个 after，空闲时仍会互相空转，是本工具要避免的。）

    同 key 再次 run() = 从 0 重新计时，天然支持"打断重来"（如进度条连续更新）。
    """

    def __init__(self, root):
        self.root = root
        self._items = {}       # key -> [t0, dur, on_frame, on_done]
        self._running = False
        self._seq = 0

    def run(self, dur, on_frame, on_done=None, key=None):
        if key is None:
            self._seq += 1
            key = "_a%d" % self._seq
        self._items[key] = [time.monotonic(), max(0.0, float(dur)), on_frame, on_done]
        if not self._running:
            self._running = True
            self.root.after(ANIM_FPS_MS, self._tick)
        return key

    def cancel(self, key):
        self._items.pop(key, None)

    def cancel_prefix(self, prefix):
        for k in [k for k in list(self._items)
                  if isinstance(k, str) and k.startswith(prefix)]:
            self._items.pop(k, None)

    def busy(self):
        return bool(self._items)

    def _tick(self):
        now = time.monotonic()
        for key, item in list(self._items.items()):
            t0, dur, on_frame, on_done = item
            # dur 单位是毫秒，time.monotonic() 差值是秒 —— 必须换算，否则动画慢 1000 倍
            p = 1.0 if dur <= 0 else (now - t0) / (dur / 1000.0)
            done = p >= 1.0
            if done:
                p = 1.0
            try:
                on_frame(p)
            except Exception as e:
                _log("anim frame err %r: %r" % (key, e))
                done = True
            if done:
                self._items.pop(key, None)
                if on_done:
                    try:
                        on_done()
                    except Exception as e:
                        _log("anim done err %r: %r" % (key, e))
        if self._items:
            self.root.after(ANIM_FPS_MS, self._tick)
        else:
            self._running = False


# ================================================================ 主线程消息队列（v2.0.2）
class MsgQueue(queue.Queue):
    """带唤醒的消息队列：**主线程** put 时顺便确认消息泵在表上。

    为什么不在后台线程里唤醒：tkinter 的任何 Tcl 调用（含 event_generate / after）
    从非主线程发起时会走 _tkinter 的「等主线程进事件循环」路径，主线程不在
    mainloop 里就会**一直阻塞**（实测：worker 卡在 put 里，后面的消息再也发不出来）。
    Tcl 文档推荐的 when="tail" 也绕不开这层 Python 侧等待。所以约定：
      · 主线程 put  -> 直接 _ensure_poll()（本来就在主线程，安全）
      · 后台线程 put-> 只入队；靠「后台任务登记」（_spawn / _hot）让泵保持心跳
    """
    _main = None

    def __init__(self, wake=None):
        queue.Queue.__init__(self)
        self._wake = wake
        self._main = threading.main_thread()

    def put(self, item, block=True, timeout=None):
        queue.Queue.put(self, item, block, timeout)
        if self._wake is None or threading.current_thread() is not self._main:
            return                      # 后台线程：绝不碰 Tk（见类注释）
        try:
            self._wake()
        except Exception:
            pass


# ================================================================ GUI
class App:
    def __init__(self, root):
        self.root = root
        self.api = AsusApi()
        self.q = MsgQueue(lambda: self._wake())   # v2.0.2：带唤醒，空闲不空转
        self._polling = False                     # 消息泵是否在表上
        self._workers = 0                         # v2.0.2：在跑的后台任务数
        self._wlock = threading.Lock()
        # 目录状态
        self.catalog = None
        self.cat_updated = ""
        self._cat_running = False   # v2.0.2：目录刷新在途标志（重入保护）
        self._cat_retry_id = None   # v2.3.1：待执行的自动重试（Tk after id）
        self._cat_retry_n = 0       # v2.3.1：已排到第几次重试
        self.cfg = load_cfg()           # 用户设置（刷新间隔 / 显示范围 / 保存目录）
        # ---- 目录（v2.2.0 三栏：左=芯片组 / 中=型号）
        self.iid_board = {}         # 型号列表 iid -> board dict（含 slug/title/url/…）
        self._chip_iid = {}         # v2.2.0：芯片组树 iid -> chipset dict（含 boards）
        self.cur_chipset = None     # v2.2.0：中栏当前展示的芯片组（None = 搜索结果平铺）
        self._iid_full = {}         # 芯片组树 iid -> 完整名（列宽不足时悬停可见）
        self._mod_iid_full = {}     # v2.2.0：型号列表 iid -> 完整型号名（悬停提示用）
        self._mod_retext_job = None  # v2.2.0：型号列表省略号重算防抖
        self._pane_last = (0, 0, 0)  # v2.2.0：三栏宽度缓存（避免 <Configure> 里反复写）
        self.iid_board = {}          # 型号列表 iid -> board dict（_populate_models 写入）
        self._chip_iid = {}          # v2.2.0：芯片组树 iid -> chipset dict
        self._chips_visible = []     # v2.2.0：当前可见芯片组（过滤后），搜索跨组平铺用
        self.cur_chipset = None      # v2.2.0：中栏当前展示的芯片组
        self._tip = None            # v2.0.1：悬停提示浮窗
        self._tip_txt = None
        self._tip_wrap = None
        self._retext_job = None
        self._tv_retext_job = None
        # v2.0.3：目录树的「平台折叠记忆」。iid 每次 _populate_tree 都会重发，
        # 所以按**完整显示标签**（"Intel 主板（554 款）"）记（跨重建稳定）。
        # v2.2.0：左栏收成两级后芯片组是叶子、没有"展开"可言，只需记折叠过的平台。
        # 记录里没有 = 用默认值（展开），语义不会被"未初始化"搅混。
        self._closed_pl = set()     # 用户折叠过的平台显示标签
        self._built_q = None        # 上一次重建时生效的搜索词（判断"上次是不是搜索态"）
        self._skip_capture = False  # 程序刚改过记忆 → 跳过下一次快照
        self._status_full = ""      # 状态栏完整消息（显示可能被省略号收尾）
        # BIOS 查询状态
        self.cur_slug = None
        self.cur_title = ""
        self.cur_host = "com"        # 当前型号 BIOS 数据源：com=全球站 / cn=中国站 / rog=ROG 站
        self.cur_src = "prod"        # 当前型号来源：prod=普通型号 / rog=ROG 系列
        self.cur_board = None        # 当前选中型号的目录条目（含 url/src/hashid/…）
        self.groups = {}
        self.badges = []
        self.cur_badge = None
        self.cur_items = []
        self._loading_slug = None   # 正在查询的 slug（防同一型号重复请求）
        # 下载
        # v2.0.2：保存目录落盘持久化（设备可保存）——「选一次就一直记得」
        _sd = self.cfg.get("save_dir")
        self.last_dir = _sd if (_sd and os.path.isdir(_sd)) else None
        self.dl_thread = None
        self.dl_cancel = threading.Event()
        # ---- v2.4.0 动效 / 交互状态
        self.anim = Anim(root)
        self.v_search = tk.StringVar()      # 搜索框内容
        self._search_ph = True              # 占位文案是否显示中
        self._search_job = None             # 搜索防抖 after id
        self._search_q = ""                 # 生效中的过滤词（小写）
        self._search_hits = 0
        self._hover_row = None              # 列表 hover 行 iid
        self._latest_iids = set()           # 「最新」行 iid 集合（hover 变体用）
        self._iid_item = {}                 # 列表 iid -> BIOS 条目（选中项直接查）
        self._loading_name = None           # 加载态标题（None = 不在加载）
        self._loading_ticks = 0             # 加载点动画计数
        self._loading_job = None
        self._dl_speed = None               # 下载速度 EMA（B/s）
        self._dl_last_t = None
        self._dl_last_b = 0
        self._toast_job = None
        self._build_ui()
        self._apply_style()
        self._bind_shortcuts()
        # v2.0.2：消息泵改「有活才排表」——首次启动排一次，之后由主线程 put
        # 或新开的后台任务（_spawn）重新排表
        self._polling = True
        self.root.after(120, self._poll_queue)
        self.root.after(180, self._apply_dark_titlebar)
        self.root.after(300, self._boot)

    # ------------------------------------------------------------- 样式（ROG 红黑 · 深色电竞主题）
    def _apply_style(self):
        self.root.configure(bg=C_BG)
        # 关键：tk_setPalette 强制把所有 tk 默认控件（含根窗体客户区）染主题色
        # 单独 root.configure(bg) 在 Windows 11 DWM 下不一定生效
        # 深色皮肤：选中态为暗红底，文字保持亮色
        try:
            self.root.tk_setPalette(
                background=C_BG, foreground=C_TEXT,
                activeBackground=C_SEL_BG, activeForeground=C_TEXT,
                selectBackground=C_SEL_BG, selectForeground=C_TEXT,
                insertBackground=C_TEXT,
            )
        except Exception:
            pass
        # 全局 option（影响 Combobox 下拉等 tk 原生 popdown）
        try:
            self.root.option_add("*TCombobox*Listbox.background", C_CARD)
            self.root.option_add("*TCombobox*Listbox.foreground", C_TEXT)
            self.root.option_add("*TCombobox*Listbox.selectBackground", C_SEL_BG)
            self.root.option_add("*TCombobox*Listbox.selectForeground", C_TEXT)
            self.root.option_add("*TCombobox*Listbox.font", FONT_S)
            self.root.option_add("*Listbox.background", C_CARD)
            self.root.option_add("*Listbox.foreground", C_TEXT)
            self.root.option_add("*Listbox.selectBackground", C_SEL_BG)
            self.root.option_add("*Listbox.selectForeground", C_TEXT)
        except Exception:
            pass
        st = ttk.Style()
        self.st = st          # v2.4.0：进度条完成脉冲需在动效帧里改 style
        # 'alt' 主题在跨平台都完全支持 background 颜色覆写（clam 在 Win 下对 TFrame/Status/Treeview 硬编码 native look）
        for tname in ("alt", "clam", "xpnative", "default"):
            try:
                st.theme_use(tname)
                break
            except Exception:
                continue
        st.configure(".", font=FONT, background=C_BG, foreground=C_TEXT)
        # ---- 容器
        st.configure("TFrame", background=C_BG)
        # v2.1.0 关键修复：卡片描边以前**从来没画出来过**。
        # Card.TFrame 一直写着 borderwidth=1 + bordercolor=C_BORDER，但 relief="flat"
        # 会让 Frame.border 元素用背景色描边 —— 等于描了个看不见的边。
        # 实测对照（扫描卡片左边缘一行的像素）：
        #     relief="flat"  → #0a0c10 #0a0c10 #12161d …   画布直接跳到卡片，无边
        #     relief="solid" → #0a0c10 #0a0c10 #252a33 #12161d …   ← C_BORDER 出现
        # 这就是旧版"一片平"的根因：两张卡片其实都溶在窗底上，没有轮廓。
        # 改 solid 后 borderwidth 不变（仍 S(1)），**不产生任何几何位移**。
        st.configure("Card.TFrame", background=C_CARD,
                     borderwidth=S(1), relief="solid", bordercolor=C_BORDER)
        # v2.1.0：**卡片内部的容器**必须用这个样式，不能用 Card.TFrame。
        # 起因：borderwidth/relief 是设在 style 上的，一改成 solid，所有用
        # Card.TFrame 的控件全都跟着画边框 —— 实测右栏多出 6 条、左栏多出 4 条
        # 不该有的横线（brow/drow/orow/pbar/lhead/twrap 全是）。
        # CardInner 保持与旧版**像素级一致**：borderwidth 仍是 S(1)（几何不变，
        # 不产生任何位移），但 relief=flat 让那 1px 用卡片底色描边 = 看不见。
        st.configure("CardInner.TFrame", background=C_CARD,
                     borderwidth=S(1), relief="flat", bordercolor=C_CARD)
        st.configure("StatusBar.TFrame", background=C_STATUS_BG)
        # v2.2.0：跨三栏底部工具条（保存目录 + 操作 + 进度条）。
        # 提到全宽的理由：路径文本在 598px 的右栏里必被截断，全宽后 ~1138px 才够；
        # 主按钮也从「右栏右下角」挪到窗口右下角，视线落点更自然。
        st.configure("ActionBar.TFrame", background=C_CARD)
        # ---- 文字
        st.configure("TLabel", background=C_BG, foreground=C_TEXT)
        st.configure("Card.TLabel", background=C_CARD, foreground=C_TEXT)
        st.configure("Sub.TLabel", background=C_CARD, foreground=C_SUB, font=FONT_S)
        st.configure("Dim.TLabel", background=C_CARD, foreground=C_DIM, font=FONT_S)
        st.configure("Title.TLabel", background=C_BG, foreground=C_TEXT, font=FONT_TITLE)
        st.configure("Ver.TLabel", background=C_BG, foreground=C_DIM, font=FONT_S)
        st.configure("Bold.TLabel", font=FONT_B, background=C_CARD, foreground=C_TEXT)
        st.configure("H.TLabel", font=FONT_H, background=C_CARD, foreground=C_TEXT)
        st.configure("Section.TLabel", font=FONT_B, background=C_CARD, foreground=C_RED_HOT)
        st.configure("Board.TLabel", font=FONT_BOARD, background=C_CARD, foreground=C_TEXT)
        st.configure("Status.TLabel", background=C_STATUS_BG, foreground=C_STATUS_FG, font=FONT_S)
        # ---- 按钮：次级（深灰底 + 描边）
        st.configure("TButton", font=FONT, background=C_BTN, foreground=C_TEXT,
                     borderwidth=S(1), bordercolor=C_BORDER, focusthickness=0,
                     lightcolor=C_BTN, darkcolor=C_BTN,
                     padding=(S(14), S(6)), relief="flat")
        st.map("TButton",
               background=[("active", C_BTN_HOVER), ("pressed", C_BTN_PRESS),
                           ("disabled", "#252a33")],
               foreground=[("active", C_TEXT), ("pressed", C_TEXT),
                           ("disabled", C_DIM)],
               bordercolor=[("active", "#4d5563"), ("disabled", "#2a2f38")])
        # ---- 按钮：主操作（ROG 红）
        # v2.3.5：红底加深一档 + hover/pressed 联动收敛，降低 ClearType 红透白字的
        # 边缘混色；字号 FONT_ACCENT(-18px) 让白字笔画更粗实（像素实测实心率↑）
        st.configure("Accent.TButton", font=FONT_ACCENT, foreground="#ffffff",
                     background=C_ACCENT_BTN, borderwidth=0, focusthickness=0,
                     padding=(S(14), S(6)), relief="flat")
        st.map("Accent.TButton",
               background=[("active", C_ACCENT_BTN_HOT), ("pressed", C_ACCENT_BTN_DEEP),
                           ("disabled", C_ACCENT_BTN_DIS)],
               foreground=[("disabled", C_ACCENT_BTN_FG_DIS)])
        # ---- 下拉框（搜索框是自绘 tk.Entry，不走 ttk 样式，故不再配 TEntry）
        st.configure("TCombobox", font=FONT, fieldbackground=C_BTN, foreground=C_TEXT,
                     arrowcolor=C_TEXT, bordercolor=C_BORDER, lightcolor=C_BTN,
                     darkcolor=C_BTN, borderwidth=S(1), relief="flat", padding=S(3))
        st.map("TCombobox",
               fieldbackground=[("readonly", C_BTN), ("focus", C_BTN)],
               foreground=[("readonly", C_TEXT)],
               bordercolor=[("focus", C_ACCENT)])
        # ---- Treeview：BIOS 表
        # v2.1.0：表格体从「与卡片同色」改为**洼地色**（C_INSET）—— 列表读作嵌进
        # 卡片里的一块凹陷区，比平铺同色更有层次；行高 32→36 让四列信息有呼吸感。
        st.configure("Treeview", font=FONT_S, rowheight=S(TV_ROWH), background=C_INSET,
                     fieldbackground=C_INSET, foreground=C_TEXT,
                     bordercolor=C_BORDER, borderwidth=S(1), relief="flat")
        # v2.1.0：表头**去边框化** —— 旧版用 FONT_B(17px 粗) + 独立底色 #20242c，
        # 像一条贴在表上的深色胶带。新版改用 13px 常规字 + 抬升条色（C_CARD_TOP）
        # + 弱化字色（C_DIM），让它读作"标注"而不是"内容"，去抢标题的注意力。
        # 字色用 C_SUB 而非 C_DIM：C_DIM 在抬升条（#171c24）上的对比度只有约 3.9:1，
        # 低于小字的无障碍线（WCAG AA 4.5:1），实测截图里表头几乎看不见；
        # C_SUB 约 7.4:1，且靠 13px 小字号 + 非粗体仍能读出"这是标注不是内容"。
        st.configure("Treeview.Heading", font=FONT_SS, background=C_HEADING,
                     foreground=C_SUB, borderwidth=0, relief="flat",
                     padding=(S(12), S(9)))
        st.map("Treeview",
               background=[("selected", C_SEL_BG)],
               foreground=[("selected", C_TEXT)])
        st.map("Treeview.Heading",
               background=[("active", C_CARD_TOP)],
               foreground=[("active", C_SUB)],
               relief=[("active", "flat")])
        # ---- 目录树（左栏：芯片组）
        # v2.2.0：左栏从「平台→芯片组→型号」三级收成**两级** —— 型号层搬到中栏，
        # 层级的"展开"变成列导航。行高 30 → 26：只剩约 33 行，可以紧凑些。
        st.configure("Cat.Treeview", font=FONT_S, rowheight=S(CAT_TREE_ROWH), background=C_INSET,
                     fieldbackground=C_INSET, foreground=C_TEXT,
                     bordercolor=C_BORDER, borderwidth=0, relief="flat",
                     # v2.3.7：indent 16→10；v2.0.4 再收紧 10→4
                     #   （用户截图反馈：芯片组下的主板型号离左边框太远）
                     # v2.1.1：改为引用 CAT_INDENT，与列宽实测公式共用同一定义
                     indent=S(CAT_INDENT))
        st.map("Cat.Treeview",
               background=[("selected", C_SEL_BG)],
               foreground=[("selected", C_TEXT)])
        # ---- 型号列表（中栏）
        # v2.2.0 新增。与 Cat.Treeview 的差别只有行高（30 vs 26）与 indent=0
        #（型号是**平铺**列表，没有层级，不需要缩进；Tk 那个 15px 展开箭头槽
        # 每个 item 仍会占位，见 CAT_IND_SLOT —— 槽去不掉，只能算进前缀开销）。
        st.configure("Mod.Treeview", font=FONT_S, rowheight=S(MOD_TREE_ROWH), background=C_INSET,
                     fieldbackground=C_INSET, foreground=C_TEXT,
                     bordercolor=C_BORDER, borderwidth=0, relief="flat",
                     indent=S(0))
        st.map("Mod.Treeview",
               background=[("selected", C_SEL_BG)],
               foreground=[("selected", C_TEXT)])
        # v2.1.1：建一个与 Cat.Treeview **同族同字号**的具名字体，供 Tcl font measure
        # 实测文本宽度（Tk 的 font 命令接受具名字体，测量值即渲染值）
        self._fm_tree = "catMeasure"
        self._px_cache = {}
        self._px_dpi = None
        try:
            self.root.tk.call("font", "delete", self._fm_tree)
        except Exception:
            pass
        try:
            self.root.tk.call("font", "create", self._fm_tree,
                              "-family", FONT_S[0], "-size", FONT_S[1])
        except Exception:
            self._fm_tree = None
        # v2.0.2：右栏型号标题用的具名字体（Board.TLabel = FONT_BOARD），
        # 供标题行溢出前实测宽度 —— 字号与目录树不同，不能共用上面的测量字体
        self._fm_board = "boardMeasure"
        try:
            self.root.tk.call("font", "delete", self._fm_board)
        except Exception:
            pass
        try:
            self.root.tk.call("font", "create", self._fm_board,
                              "-family", FONT_BOARD[0], "-size", FONT_BOARD[1])
        except Exception:
            self._fm_board = None
        # v2.3.0：次级小字（FONT_SS）的测量字体。详情面板的 SHA-256 用 FONT_SS，
        # 拿 FONT_S 的测量字体去算会高估约 15%，把"放得下"判成"放不下"。
        self._fm_ss = "ssMeasure"
        try:
            self.root.tk.call("font", "delete", self._fm_ss)
        except Exception:
            pass
        try:
            self.root.tk.call("font", "create", self._fm_ss,
                              "-family", FONT_SS[0], "-size", FONT_SS[1])
        except Exception:
            self._fm_ss = None
        # ---- 滚动条（v2.1.0 重做）
        # 旧版保留 alt 主题默认布局（两端各有一个箭头按钮）。实测问题：ttk 的
        # thumb 与 uparrow/downarrow **共用同一个 -background**，二者永远同色 ——
        # 扫描线实测两端与滑块都是 #2b313c，中间槽是 #080a0d。
        # 后果不是"难看"而是**读不出滑块位置**：滑块滚到两端时与端帽糊成一块，
        # 用户看不出当前在看哪一段。而且不显式设 lightcolor/darkcolor 时，alt 给
        # 箭头按钮留了默认亮色 3D 斜角（实测 #6f7885 亮斑）。
        #
        # 解法：重定义布局，去掉 uparrow/downarrow，只留 trough + thumb
        #（现代滚动条的做法，macOS / Windows 11 均无箭头按钮）。
        # 功能等价性已实测：点槽翻页 yview 0.400→0.550（与带箭头版**完全一致**）、
        # 拖拽滑块、set()、滚轮均正常；仅少了两个"点一下翻一格"的冗余按钮。
        try:
            st.layout("Vertical.TScrollbar", [
                ("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
                    ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
        except Exception:
            pass
        st.configure("Vertical.TScrollbar", background=C_SCROLL, troughcolor=C_TROUGH,
                     bordercolor=C_TROUGH, arrowcolor=C_DIM,
                     lightcolor=C_SCROLL, darkcolor=C_SCROLL,
                     borderwidth=0, relief="flat", width=S(11))
        st.map("Vertical.TScrollbar",
               background=[("active", "#4c5665"), ("pressed", "#5b6676")])
        # ---- 进度条
        # v2.1.0：lightcolor/darkcolor 从「亮红/暗红」改为与槽同色 —— 旧配置让空的
        # 进度条画出一圈亮边，读作一个"空的输入框"而不是"一条待填的槽"。
        # 抹平斜角后：空态 = 一条安静的凹槽，有值 = 一段纯红填充。
        # thickness 收细 17→10，让它退成辅助元素，不与上面的表格抢注意力。
        try:
            st.configure("TProgressbar", thickness=S(10))
        except Exception:
            pass
        st.configure("TProgressbar", troughcolor=C_TROUGH, background=C_ACCENT,
                     bordercolor=C_TROUGH, lightcolor=C_TROUGH,
                     darkcolor=C_TROUGH)
        # 列表行 tag
        try:
            # v2.1.0：普通行底跟随洼地色（与 Treeview 的 fieldbackground 一致，
            # 否则 tag 会把洼地重新盖回卡片色，分层失效）
            self.tv.tag_configure("row", background=C_INSET, foreground=C_TEXT)
            # 最新版本行：深绿底 + 亮绿字（深色主题下用亮色保证对比度）
            self.tv.tag_configure("latest", background=C_GREEN_BG,
                                  foreground=C_GREEN, font=FONT_B)
            # v2.4.0：hover 变体（普通行 / 最新行各一套，避免多 tag 优先级不确定）
            self.tv.tag_configure("rowh", background=C_ROW_HOVER, foreground=C_TEXT)
            self.tv.tag_configure("latest_h", background=C_LATEST_HOVER,
                                  foreground=C_GREEN, font=FONT_B)
            # latest 非选中时显绿底，选中时由 selected map 接管成暗红
            try:
                st.map("Treeview",
                       background=[("selected", C_SEL_BG)],
                       foreground=[("selected", C_TEXT)])
            except Exception:
                pass
        except Exception:
            pass
        # 左栏芯片组树行 tag：平台红色加粗 / 芯片组亮字
        #（v2.2.0：型号层已搬到中栏，左栏不再有 board 行）
        try:
            self.cat_tree.tag_configure("platform", foreground=C_RED_HOT, font=FONT_B)
            self.cat_tree.tag_configure("chipset", foreground=C_TEXT)
        except Exception:
            pass
        # 中栏型号列表行 tag（v2.2.0）：平铺列表，只有一种行
        try:
            self.mod_tree.tag_configure("board", foreground=C_SUB)
        except Exception:
            pass

    def _card(self, parent, **kw):
        # v2.1.0：垂直内边距 16→15/14 收紧一档（内容更贴卡片边、卡片显大）。
        # v2.2.0：水平保持 S(16) —— `PICKER_CARD_PAD = 16×2 + 1×2 = 34` 正是按
        # 这个值定的（卡片宽 − 内外边距与边框 − 滚动条 = Treeview #0 列宽），
        # 改这里必须同步改 PICKER_CARD_PAD，否则列宽会差几个像素。
        return ttk.Frame(parent, style="Card.TFrame",
                         padding=(S(16), S(15), S(16), S(14)))

    # v2.2.0：三栏宽度实测自适应相关（见 _chip_col_need / _text_px / _layout_panes）
    _need_key = None      # 列宽需求的缓存键（DPI + 目录版本）
    _need_v = 0           # 缓存的列宽需求值
    _px_dpi = None        # 文本宽度缓存的 DPI 标记
    _px_cache = None      # {文本: 像素宽}
    _fm_tree = None       # 测量用 Tcl 具名字体（与 Cat.Treeview 同族同字号）
    _fm_board = None      # v2.0.2：右栏标题测量字体（与 Board.TLabel 同族同字号）
    _fm_ss = None         # v2.3.0：次级小字测量字体（与 FONT_SS 同族同字号）
    _cat_col = 0          # v2.2.0：左栏（芯片组）#0 列宽（_layout_panes 结果）
    _mod_col = 0          # v2.2.0：中栏（型号）#0 列宽（_layout_panes 结果）
    _body_pad = None      # v2.0.5：布局区容器左右内边距 + 边框（实测缓存，见 _body_pane_pad）
    _laying_cols = False  # v2.0.5：BIOS 列表列宽重排的重入守卫（见 _layout_tv_columns）
    _iid_full = None      # v2.0.1：iid -> (完整名, 层级, 是否有子节点)
    _tip = None           # v2.0.1：悬停提示浮窗（Toplevel）
    _tip_txt = None       # 当前提示内容（同一行不重建浮窗，免闪烁）
    _retext_job = None    # v2.0.1：列宽变化后重算省略号的防抖 after id
    _title_full = ""      # v2.0.2：右栏标题完整文本（显示用的可能带省略号）
    _tv_retext_job = None  # v2.0.2：列表说明列变宽/变窄后重算省略号的防抖 after id
    _tip_wrap = None      # v2.0.3：当前提示浮窗的换行宽（长说明用，None=单行）
    _status_full = ""     # v2.0.3：状态栏完整消息（显示用的可能被省略号收尾）
    _fitting_status = False  # v2.0.3：状态文本自适应中的重入守卫

    # ------------------------------------------------------------- UI 布局
    def _build_ui(self):
        root = self.root
        root.title("%s v%s" % (APP_TITLE, APP_VERSION))
        # v2.0.0：源码运行时也带上窗口 / 任务栏图标（打包后由 exe 资源提供）
        try:
            _ico = os.path.join(_app_dir(), "asus_bios.ico")
            if os.path.isfile(_ico):
                root.iconbitmap(_ico)
        except Exception:
            pass
        _win_w, _win_h = _fit_size(1200, 780, 40)
        root.geometry("%dx%d" % (_win_w, _win_h))
        root.minsize(min(S(1100), _win_w), min(S(760), _win_h))
        root.configure(bg=C_BG)
        # 天窗：ttk 主题下 root 客户区仍走系统浅色，铺一个全屏 tk.Frame 充当天窗
        self._sky = tk.Frame(root, bg=C_BG)
        self._sky.pack(fill="both", expand=True)

        # ---------- 顶部品牌红条
        # v2.1.0：4→3px 收细一档，配合下面标题字号下调，整条标题区更"精工"
        self._brand = tk.Frame(self._sky, bg=C_BRAND, height=S(3))
        self._brand.pack(fill="x", side="top")

        # ---------- 标题区
        # v2.2.0：垂直内边距 (14,6) → (9,9)、logo 24→22、标题字号 22→19。
        # 整条标题区从 74px 压到 46px —— 它只承载「工具名 / 版本 / 公司 / 设置」
        # 四条信息，却曾占掉窗口高度的 10%，而下面的正文区才是干活的地方。
        head = ttk.Frame(self._sky, padding=(S(20), S(7), S(20), S(7)))
        head.pack(fill="x")
        self._head = head
        # v2.1.0：logo 竖条 10×28 → 4×24 —— 旧版是一块"红方块"，改细线后
        # 读作品牌标记而非色块（ROG 视觉语言里的竖向强调线）
        logo = tk.Frame(head, bg=C_BRAND, width=S(4), height=S(22))
        logo.pack(side="left", padx=(S(0), S(12)))
        logo.pack_propagate(False)
        ttk.Label(head, text=APP_TITLE, style="Title.TLabel").pack(side="left")
        self.btn_settings = ttk.Button(head, text="设置", width=6,
                                       command=self._open_settings)
        self.btn_settings.pack(side="right", padx=(S(16), S(0)))
        ttk.Label(head, text="v%s   ·   %s" % (APP_VERSION, APP_COMPANY),
                  style="Ver.TLabel").pack(side="right")

        # ---------- 状态栏（先 pack，让底部空间留给状态栏，避免被 body expand 挤掉）
        # v2.1.0：分隔线 C_BORDER→C_HAIR —— 区域分隔用更弱的发丝线，描边级别的
        # #252a33 留给卡片轮廓，避免"每条线都一样重"
        tk.Frame(self._sky, bg=C_HAIR, height=S(1)).pack(fill="x", side="bottom")
        sb = ttk.Frame(self._sky, style="StatusBar.TFrame", padding=(S(20), S(8)))
        sb.pack(fill="x", side="bottom")
        self.v_status = tk.StringVar(value="就绪")
        # v2.4.0：左侧状态圆点（颜色随语义变化）+ 状态文字（完成时做闪烁脉冲）
        self.dot_status = tk.Label(sb, text="●", font=FONT_S,
                                   bg=C_STATUS_BG, fg=C_DIM)
        self.dot_status.pack(side="left", padx=(S(0), S(8)))
        self.lbl_status = tk.Label(sb, textvariable=self.v_status, font=FONT_S,
                                   bg=C_STATUS_BG, fg=C_STATUS_FG, anchor="w")
        self.lbl_status.pack(side="left")
        # 右侧常驻：数据源 + 版本（原来完全没有，用户不知道当前查的是哪个站）
        self.v_stat_right = tk.StringVar(value="数据源：—   ·   %s" % _ver_suffix())
        self.lbl_stat_right = tk.Label(sb, textvariable=self.v_stat_right, font=FONT_S,
                                       bg=C_STATUS_BG, fg=C_DIM)
        self.lbl_stat_right.pack(side="right")
        # v2.0.3：状态栏长文本防御 —— 保存 BIOS 后的提示带着完整文件路径
        # （实测 92 字 = 879px，最小窗口下只剩几十像素余量；路径再长就顶出去，
        # 右侧「数据源 · 版本」会被挤掉）。按可用宽省略收尾，完整消息悬停可见。
        self.lbl_status.bind("<Configure>", lambda _e: self._refit_status(), add="+")
        self.lbl_status.bind("<Enter>", self._status_tip, add="+")
        self.lbl_status.bind("<Leave>", lambda _e: self._hide_tip(), add="+")
        sb.bind("<Configure>", lambda _e: self._refit_status(), add="+")

        # ---------- 跨三栏底部工具条（v2.2.0 新增）
        # 旧版把「保存目录」放在一行、「选择…/打开目录 | 取消下载/下载选中版本」放在
        # 下一行：路径的操作与所选版本的操作被挤在同一行（靠中间弹性空隙隔开），
        # 而保存目录的路径本身被单独扔在上一行 —— 同一件事被拆成两行，两件不同的
        # 事被挤成一行。这里按语义重排：行 0 = 路径（吃掉全部剩余宽度）+ 路径操作
        # | 全局动作右对齐；行 1 = 进度条 + 速度/剩余时间。
        # 同时升为**跨三栏的全宽条**：路径文本在 598px 的右栏里必被截断，全宽后
        # 才有 ~1138px 可用；主按钮也从「右栏右下角」挪到窗口右下角，落点更自然。
        tk.Frame(self._sky, bg=C_HAIR, height=S(1)).pack(fill="x", side="bottom")
        abar = ttk.Frame(self._sky, style="ActionBar.TFrame",
                         padding=(S(20), S(8), S(20), S(6)))
        abar.pack(fill="x", side="bottom")
        self._abar = abar
        abar.columnconfigure(0, weight=1)
        arow = ttk.Frame(abar, style="ActionBar.TFrame")
        arow.grid(row=0, column=0, sticky="ew")
        # 列 0=标签 1=路径(弹性) 2=选择… 3=打开目录 4=取消下载 5=下载选中版本
        # v2.0.0 保留：同一 grid 行内 sticky="nsew" 会把所有按钮拉到**同一行高**
        #（旧版用 pack，红主按钮 FONT_ACCENT 比 Ghost 按钮高 7px，居中后上下底边
        # 各差 3~4px —— 正是用户反馈的「上下没对齐」）。
        arow.columnconfigure(1, weight=1)
        arow.rowconfigure(0, weight=1)
        ttk.Label(arow, text="保存目录：", style="Sub.TLabel").grid(
            row=0, column=0, sticky="w")
        # v2.3.0：启动时就把已知的保存目录写进来。旧版这里恒为 ""，只有真正
        # 开始下载时才 set —— 结果 cfg.save_dir 里明明持久化着一个目录，
        # 界面却显示「保存目录：」后面一片空白，用户到点下载前都不知道文件
        # 会落到哪。没有持久化目录时给一句占位说明（留白会被读成"坏了"）。
        self.v_dir = tk.StringVar(value=self.last_dir or "首次下载时选择…")
        # 路径本身可点（原来必须点「打开目录」按钮，路径只是死文字）
        self.lnk_dir = tk.Label(arow, textvariable=self.v_dir, font=FONT_S,
                                bg=C_CARD, fg=C_SUB, cursor="hand2", anchor="w")
        self.lnk_dir.grid(row=0, column=1, sticky="ew", padx=(S(2), S(12)))
        self.lnk_dir.bind("<Button-1>", lambda e: self._on_open_dir())
        self._bind_hover_fg(self.lnk_dir, C_SUB, C_BLUE)
        # 次级操作统一 Ghost 风格（深灰底 + 细边），与红主 / 蓝链形成三态
        self.btn_pick = tk.Button(arow, text="选择…", font=FONT_S,
                                  bg=C_BTN, fg=C_TEXT,
                                  activebackground=C_BTN_HOVER,
                                  activeforeground=C_TEXT,
                                  relief="solid", bd=S(1),
                                  padx=S(16), pady=S(8), cursor="hand2",
                                  command=self._on_pick_dir)
        self.btn_pick.grid(row=0, column=2, sticky="nsew")
        self.btn_open_dir = tk.Button(arow, text="打开目录", font=FONT_S,
                                      bg=C_BTN, fg=C_TEXT,
                                      activebackground=C_BTN_HOVER,
                                      activeforeground=C_TEXT,
                                      relief="solid", bd=S(1),
                                      padx=S(16), pady=S(8), cursor="hand2",
                                      command=self._on_open_dir)
        self.btn_open_dir.grid(row=0, column=3, sticky="nsew", padx=(S(8), S(0)))
        # 取消下载（仅下载中显示；与红主按钮同排，grid_remove/grid 切换）
        self.btn_cancel = tk.Button(arow, text="取消下载", font=FONT_S,
                                    bg=C_BTN, fg=C_TEXT,
                                    activebackground=C_BTN_HOVER,
                                    activeforeground=C_ACCENT_HOT,
                                    relief="solid", bd=S(1),
                                    padx=S(16), pady=S(8), cursor="hand2",
                                    command=self._on_cancel_download)
        # 主操作：ROG 红（tk.Button，避开 vista 主题 ttk.Button 的 ■ 装饰）
        self.btn_dl = tk.Button(arow, text="下载选中版本", font=FONT_ACCENT,
                                bg=C_ACCENT_BTN, fg="#ffffff",
                                activebackground=C_ACCENT_BTN_HOT,
                                activeforeground="#ffffff",
                                disabledforeground=C_ACCENT_BTN_FG_DIS,
                                relief="flat", bd=0,
                                padx=S(20), pady=S(8), cursor="hand2",
                                command=self._on_download)
        self.btn_dl.grid(row=0, column=5, sticky="nsew", padx=(S(10), S(0)))
        for _b, _base, _hot in ((self.btn_pick, C_BTN, C_BTN_HOVER),
                                (self.btn_open_dir, C_BTN, C_BTN_HOVER),
                                (self.btn_cancel, C_BTN, C_BTN_HOVER),
                                (self.btn_dl, C_ACCENT_BTN, C_ACCENT_BTN_HOT)):
            self._bind_hover(_b, _base, _hot)
        # 行 1：进度条 + 速度 / 剩余时间
        prow = ttk.Frame(abar, style="ActionBar.TFrame")
        prow.grid(row=1, column=0, sticky="ew", pady=(S(6), S(0)))
        self._prow = prow
        self.pb = ttk.Progressbar(prow, maximum=100, value=0)
        self.pb.pack(fill="x", side="left", expand=True)
        self.v_pb = tk.StringVar(value="")
        # v2.4.0 修复：width 30→46（原 30 字符不够装下「8.2 / 17.5 MB   2.4 MB/s
        # 剩 00:04   (47%)」~47 字符，被裁为「(4…」让用户看不到完整速度/剩余时间）
        #
        # v2.1.0 改 ttk.Label → tk.Label：**空文本的 ttk.Label 不按 style 的背景色绘制**。
        # 实测：一个 text="" 的 Sub.TLabel（style 声明 background=#12161d）渲染出来是
        # #0a0c10（画布色）—— 逐像素扫描见「标签外 3px = #12161d，标签内 3px = #0a0c10」。
        # tk.Label 有真正的 -background 部件选项，空文本也照画。
        self.lbl_pb = tk.Label(prow, textvariable=self.v_pb, font=FONT_S,
                               bg=C_CARD, fg=C_SUB, width=46, anchor="e")
        self.lbl_pb.pack(side="left", padx=(S(12), S(0)))
        # v2.3.0：空闲时不显示空进度槽（见 _pb_idle 的注释）
        self._pb_idle()

        # ---------- 正文区：三栏（芯片组 / 型号 / BIOS 明细）
        body = ttk.Frame(self._sky, padding=(S(20), S(8), S(20), S(6)))
        body.pack(fill="both", expand=True)
        # 列 0/1 固定宽（由 _layout_panes() 显式设定卡片 -width 驱动），列 2 吃剩余
        body.columnconfigure(0, weight=0)
        body.columnconfigure(1, weight=0)
        body.columnconfigure(2, weight=1)
        body.rowconfigure(0, weight=1)
        self._body = body

        # ===== 左：芯片组（两级树：平台 → 芯片组）=====
        cf = self._card(body)
        cf.grid(row=0, column=0, sticky="nsew", padx=(S(0), S(PANE_GAP)))
        # 宽度由 _layout_panes() 显式设定（必须关闭 grid 传播：默认开启时 -width
        # 会被忽略；顺带一提原 pack_propagate 对 grid 子控件无效，所以旧写法
        # lf.configure(width=356) 一直是空操作）。
        # 也不能靠"列宽经传播撑出卡片"：Tk 的 Treeview 在控件已映射后改列宽时
        # winfo_reqwidth() 不更新（实测列宽 351 而 reqwidth 仍 224）。
        cf.grid_propagate(False)
        cf.columnconfigure(0, weight=1)
        cf.rowconfigure(1, weight=1)      # 0=标题 1=芯片组树 2=目录日期
        self._cf = cf
        chead = ttk.Frame(cf, style="CardInner.TFrame")
        chead.grid(row=0, column=0, sticky="ew")
        ttk.Label(chead, text="芯片组", style="H.TLabel").pack(side="left")
        # v2.4.0：目录刷新按钮（原来必须进设置窗才能刷新，多两步操作）
        self.btn_cat_refresh = tk.Button(chead, text="↻", font=FONT_B,
                                         bg=C_CARD, fg=C_SUB,
                                         activebackground=C_BTN_HOVER,
                                         activeforeground=C_ACCENT_HOT,
                                         relief="flat", bd=0, padx=S(6), pady=S(0),
                                         cursor="hand2",
                                         command=lambda: self._refresh_now(None))
        self.btn_cat_refresh.pack(side="right")
        self._bind_hover_fg(self.btn_cat_refresh, C_SUB, C_ACCENT_HOT)
        # v2.0.3：目录树「展开全部 / 折叠全部」一键切换。
        # v2.2.0：左栏收成两级后按钮作用于**平台层**（芯片组已是叶子），
        # 记忆语义相应从「记展开过的芯片组」简化为「记折叠过的平台」。
        self.btn_cat_all = tk.Button(chead, text="展开全部", font=FONT_SS,
                                     bg=C_CARD, fg=C_SUB,
                                     activebackground=C_BTN_HOVER,
                                     activeforeground=C_ACCENT_HOT,
                                     relief="flat", bd=0, padx=S(4), pady=S(0),
                                     cursor="hand2",
                                     command=self._toggle_all_cs)
        self.btn_cat_all.pack(side="right", padx=(S(0), S(12)))
        self._bind_hover_fg(self.btn_cat_all, C_SUB, C_ACCENT_HOT)

        # 芯片组树（v2.2.0：只到芯片组层，型号层已搬到中栏）
        cwrap = ttk.Frame(cf, style="CardInner.TFrame")
        cwrap.grid(row=1, column=0, sticky="nsew", pady=(S(8), S(4)), padx=(S(8), S(0)))
        cwrap.columnconfigure(0, weight=1)
        cwrap.rowconfigure(0, weight=1)
        self.cat_tree = ttk.Treeview(cwrap, show="tree", selectmode="browse",
                                     height=16, style="Cat.Treeview")
        # 列宽 = 卡片宽 − 卡片内边距边框 − 滚动条（左栏是固定宽栏，见 _layout_panes）。
        # stretch=False 是关键：Treeview 的列 stretch 默认为 1，控件当前宽度小于列宽时
        # Tk 会把可伸缩列压回控件宽度（且不低于 minwidth），使设定值立刻被打回。
        self.cat_tree.column(
            "#0", width=S(PICKER_CHIP_W - PICKER_CARD_PAD - TREE_SCROLL_W),
            minwidth=S(120), stretch=False)
        self.cat_tree.grid(row=0, column=0, sticky="nsew")
        self.cat_tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        # v2.0.1：超长名被省略号截断后，悬停显示完整名称
        self._bind_tree_tip()
        cys = ttk.Scrollbar(cwrap, orient="vertical", command=self.cat_tree.yview)
        cys.grid(row=0, column=1, sticky="ns")
        self.cat_tree.configure(yscrollcommand=cys.set)
        # 说明：不加横向滚动条 —— Treeview 的裁切发生在**列内部**（文字超出列宽即被
        # 裁掉），xview 只能滚动到视口外的列，对列内裁切无效（实测 xview 恒为
        # (0,1)）。因此正确做法是让列宽足够，而不是加横滚条。
        # 注意：目录数据时间（v_cat_upd）**只在中栏页脚**显示一处 —— 旧版放在左栏底部，
        # 三栏后若两边都放就会出现同一条信息两处重复。

        # ===== 中：型号（平铺列表）=====
        mf = self._card(body)
        mf.grid(row=0, column=1, sticky="nsew", padx=(S(0), S(PANE_GAP)))
        mf.grid_propagate(False)
        mf.columnconfigure(0, weight=1)
        mf.rowconfigure(2, weight=1)      # 0=标题 1=搜索 2=型号树 3=无结果 4=页脚
        self._mf = mf
        mhead = ttk.Frame(mf, style="CardInner.TFrame")
        mhead.grid(row=0, column=0, sticky="ew")
        ttk.Label(mhead, text="型号", style="H.TLabel").pack(side="left")
        # 右侧标注当前列表的范围（选中的芯片组名 / 搜索结果），起方位提示作用
        self.v_scope = tk.StringVar(value="")
        tk.Label(mhead, textvariable=self.v_scope, font=FONT_SS, bg=C_CARD,
                 fg=C_SUB, anchor="e").pack(side="right")

        # 搜索框（v2.4.0 新增；v2.2.0 从旧左栏搬到中栏 —— 它过滤的就是这一栏的型号）
        self._search_wrap = tk.Frame(mf, bg=C_CARD)
        self._search_wrap.grid(row=1, column=0, sticky="ew", pady=(S(8), S(6)))
        self.ent_search = tk.Entry(self._search_wrap, textvariable=self.v_search,
                                   font=FONT_S, bg=C_INSET, fg=C_DIM,
                                   insertbackground=C_TEXT, relief="flat", bd=0,
                                   highlightthickness=S(1),
                                   highlightbackground=C_BORDER,
                                   highlightcolor=C_ACCENT)
        self.ent_search.pack(fill="x", ipady=S(5), ipadx=S(5))
        self.v_search.set(SEARCH_PH)
        self.ent_search.bind("<FocusIn>", self._search_focus_in)
        self.ent_search.bind("<FocusOut>", self._search_focus_out)
        self.v_search.trace_add("write", self._on_search_changed)

        mwrap = ttk.Frame(mf, style="CardInner.TFrame")
        mwrap.grid(row=2, column=0, sticky="nsew", pady=(S(0), S(4)), padx=(S(8), S(0)))
        mwrap.columnconfigure(0, weight=1)
        mwrap.rowconfigure(0, weight=1)
        self.mod_tree = ttk.Treeview(mwrap, show="tree", selectmode="browse",
                                     height=14, style="Mod.Treeview")
        self.mod_tree.column(
            "#0", width=S(PICKER_MODEL_W - PICKER_CARD_PAD - TREE_SCROLL_W),
            minwidth=S(160), stretch=False)
        self.mod_tree.grid(row=0, column=0, sticky="nsew")
        self.mod_tree.bind("<<TreeviewSelect>>", self._on_mod_select)
        self._bind_mod_tip()
        mys = ttk.Scrollbar(mwrap, orient="vertical", command=self.mod_tree.yview)
        mys.grid(row=0, column=1, sticky="ns")
        self.mod_tree.configure(yscrollcommand=mys.set)

        # 搜索无结果提示（v2.4.0）：目录里找不到时给一条去官网的出路
        self._nores = tk.Frame(mf, bg=C_CARD)
        self.lnk_site_search = tk.Label(self._nores, text="目录中未找到 → 去官网搜索 ↗",
                                        font=FONT_S, bg=C_CARD, fg=C_BLUE,
                                        cursor="hand2")
        self.lnk_site_search.pack(anchor="w", padx=(S(10), S(0)))
        self.lnk_site_search.bind("<Button-1>", lambda e: self._on_site_search())
        self._nores.grid(row=3, column=0, sticky="w", pady=(S(2), S(2)))
        self._nores.grid_remove()

        # 页脚：目录计数（左）+ 目录数据时间（右）—— v2.2.0 从旧左栏底部搬来
        mfoot = ttk.Frame(mf, style="CardInner.TFrame")
        mfoot.grid(row=4, column=0, sticky="ew", pady=(S(0), S(2)))
        self.v_cat = tk.StringVar(value="加载中…")
        tk.Label(mfoot, textvariable=self.v_cat, font=FONT_SS, bg=C_CARD,
                 fg=C_SUB).pack(side="left", padx=(S(10), S(0)))
        # v2.2.0：目录数据时间从旧左栏底部**唯一地**搬到这里（只此一处，不再重复）。
        # 必须用 tk.Label 而非 ttk.Label：**空文本的 ttk.Label 不按 style 背景色绘制**
        #（v2.1.0 的 A25.8 就是这条），而 v_cat_upd 在目录加载前是空串。
        self.v_cat_upd = tk.StringVar(value="")
        tk.Label(mfoot, textvariable=self.v_cat_upd, font=FONT_SS, bg=C_CARD,
                 fg=C_DIM).pack(side="right", padx=(S(0), S(2)))
        # 窗口尺寸变化 → 重算三栏宽度（实测值有缓存，重算很轻）
        self._body.bind("<Configure>", lambda _e: self._layout_panes(), add="+")

        # ===== 右：BIOS 明细（列表 + 选中版本详情）=====
        rf = self._card(body)
        rf.grid(row=0, column=2, sticky="nsew")
        self._rf = rf
        # v2.4.0 关键修复：右栏改 grid 布局。grow/_panel_note_box/list_holder 都
        # 想放在 rf row=2，但 grid 不允许同 row 多 widget。
        # 解决：建一个 inner container 放 rf row=2 expand=True（占据 BIOS 列表主区），
        # grow/_panel_note_box/list_holder 改为 pack 进 container 内部（顺序可控）。
        #
        # v2.2.0：右栏纵向拆成「列表 → 详情」两段：**列表吃全部剩余高度
        # （weight=1），详情按内容自撑（weight=0 + minsize）**。
        # 不用固定比例分配——实测 grid 的 weight 只分配「超出请求尺寸的余量」，
        # 而 tv 的请求高（height=8 → 约 286px）与详情请求高（约 150px）并不为零，
        # 按 13:7 权重算出来的实际比例会走样（实测 254:193 = 1.32:1 而非 1.86:1）。
        # 让列表独占权重后：列表 = 可用高 − 详情实际高，天然自适应 ——
        # 更新说明越长详情越高、列表相应让位，无需任何硬编码比例。
        # row 0: trow, row 1: brow, row 2: _list_zone, row 3: 分隔线,
        # row 4: _detail_zone, row 5: _toast
        rf.columnconfigure(0, weight=1)
        rf.rowconfigure(2, weight=1, minsize=S(RIGHT_LIST_MIN))
        rf.rowconfigure(4, weight=0, minsize=S(RIGHT_DETAIL_MIN))
        self._list_zone = ttk.Frame(rf, style="CardInner.TFrame")
        self._list_zone.grid(row=2, column=0, sticky="nsew", pady=(S(8), S(0)))

        # 型号标题行
        trow = ttk.Frame(rf, style="CardInner.TFrame")
        trow.grid(row=0, column=0, sticky="ew")
        self._trow_board = trow
        self.v_board_title = tk.StringVar(value="BIOS 明细")
        self._lbl_board_title = ttk.Label(trow, textvariable=self.v_board_title,
                                          style="Board.TLabel")
        self._lbl_board_title.pack(side="left")
        # v2.3.6：Link 风格（背景=C_CARD 与主板卡同色，蓝色文字+↗ 外链箭头）
        self.btn_openpage = tk.Button(trow, text="打开官网支持页  ↗",
                                      font=FONT_S, bg=C_CARD, fg=C_BLUE,
                                      activebackground=C_CARD,
                                      activeforeground=C_ACCENT_HOT,
                                      relief="flat", bd=0, cursor="hand2",
                                      padx=S(10), pady=S(4),
                                      command=self._on_open_site)
        self.btn_openpage.pack(side="right", padx=(S(8), S(0)), pady=(S(6), S(0)))
        self._bind_hover_fg(self.btn_openpage, C_BLUE, C_ACCENT_HOT)
        # v2.0.2：型号标题超长时不再把右侧按钮挤没 —— 窗口一变宽窄就重算可用宽度
        trow.bind("<Configure>", lambda _e: self._fit_board_title(), add="+")
        self._lbl_board_title.bind(
            "<Enter>", lambda e: self._tip_board_title(e), add="+")
        self._lbl_board_title.bind("<Leave>", lambda _e: self._hide_tip(), add="+")

        # 副标题行：slug + 数据源徽章
        brow = ttk.Frame(rf, style="CardInner.TFrame")
        brow.grid(row=1, column=0, sticky="ew")
        self._brow_board = brow
        self.v_board_sub = tk.StringVar(value="")
        # v2.3.0：空文本的 ttk.Label 不按 style 的背景色绘制（同 v2.1.0 的 lbl_pb，
        # 实测渲成画布色 #0a0c10）。副标题在「未选型号」时为空，于是右栏标题下
        # 会留下一条 4×24 的深色竖痕 —— 截图里像个光标残影。改 tk.Label 后
        # 空文本也照卡片底色画，痕迹消失。
        self._lbl_board_sub = tk.Label(brow, textvariable=self.v_board_sub,
                                       font=FONT_S, bg=C_CARD, fg=C_SUB,
                                       anchor="w")
        self._lbl_board_sub.pack(side="left")
        self.lbl_host = tk.Label(brow, text="", font=FONT_SS, bg=C_CARD, fg=C_DIM)
        self.lbl_host.pack(side="left", padx=(S(10), S(0)))

        # ---------- 主板级提示（官方 SOP / 升级须知 / YouTube 教程）
        self.v_panel_note = tk.StringVar(value="")
        self._panel_note_box = tk.Frame(self._list_zone, bg=C_BLUE_BG,
                                        highlightbackground="#2c4a66",
                                        highlightthickness=S(1))
        self._panel_note_lbl = tk.Label(self._panel_note_box, textvariable=self.v_panel_note,
                                        font=FONT_S, bg=C_BLUE_BG, fg="#9ec9f0",
                                        justify="left", wraplength=S(720), anchor="w")
        self._panel_note_lbl.pack(fill="x", padx=S(10), pady=S(6))
        # v2.0.3：换行宽跟随提示框实际宽度（旧版写死 S(720)，窗口拉宽时文本提前
        # 换行留一条空白，窗口压窄到提示框比 720 还窄时句子尾部被裁）
        self._panel_note_box.bind("<Configure>", self._on_note_box_configure, add="+")
        self._panel_note_box.pack_forget()

        # BIOS 类型分组（通常只有 1 组，多组时才显示）
        # v2.4.0：grow 改放进 _list_zone，rf 用 grid 布局
        self.grow = ttk.Frame(self._list_zone, style="CardInner.TFrame")
        self.v_grp = tk.StringVar()
        self.grp_box = ttk.Combobox(self.grow, textvariable=self.v_grp, state="readonly",
                                    width=30, font=FONT_S)
        self.grp_box.pack(side="left")
        self.grp_box.bind("<<ComboboxSelected>>", self._on_group_changed)

        # ---------- 列表宿主（v2.4.0 结构修复）
        # 原来 Treeview 与空状态占位直接在 rf 里 pack/forget，重新 pack 时会被
        # 追加到 pack 顺序末尾 —— 列表会跑到下载区下方。改为固定宿主容器，
        # 只在其内部切换 Treeview / 占位，位置永远稳定。
        self._list_holder = ttk.Frame(self._list_zone, style="CardInner.TFrame")
        # v2.4.0：list_holder 在 _list_zone 内 expand=True（占据 _list_zone 剩余空间）
        self._list_holder.pack(fill="both", expand=True)

        cols = ("ver", "date", "size", "note")
        # v2.2.0：height 只决定**请求高**，不是显示行数（外面套了 expand=True 的宿主）。
        # 必须给小值：`_list_zone` 里还有「BIOS 类型分组」下拉与「主板级提示框」两个
        # 先 pack 的兄弟，请求高之和一旦超过列表区高度，Tk 会把它们挤成 1px
        #（实测 height=8 时提示框 h=6、标签 h=1、文字完全不显示）。
        self.tv = ttk.Treeview(self._list_holder, columns=cols, show="headings",
                               selectmode="browse", height=3)
        # 列宽基准统一取自 TV_COL_BASE（v2.2.0 收窄：168/110/92 → 140/104/80）——
        # 旧版把同一组数字在这里又写了一遍，改常量时极易漏掉这一处。
        # 实际宽度随后由 _layout_tv_columns() 按控件实测宽度重排（说明列吃剩余）。
        _colbase = dict(TV_COL_BASE)
        for cid, txt, anchor in (
                ("ver", "版本", "w"),
                ("date", "发布日期", "center"),
                ("size", "大小", "center"),
                ("note", "更新说明", "w")):
            w = S(_colbase.get(cid, TV_NOTE_MIN))
            self.tv.heading(cid, text=txt)
            self.tv.column(cid, width=w, anchor=anchor, stretch=(cid == "note"))
        self.tv.pack(fill="both", expand=True)
        self.tv.bind("<Double-1>", lambda e: self._on_tv_double_click())
        # v2.2.0：选中行 → 右栏下半的详情面板同步显示该版本的完整信息
        self.tv.bind("<<TreeviewSelect>>", lambda e: self._show_detail(
            self._iid_item.get((self.tv.selection() or [""])[0])))
        # v2.0.2：说明列是 stretch 列，窗口一变宽窄就得重算每行的省略号预算
        self.tv.bind("<Configure>", self._on_tv_configure, add="+")
        # v2.4.0：行 hover 高亮 + 右键菜单
        self.tv.bind("<Motion>", self._on_tv_motion)
        self.tv.bind("<Leave>", self._on_tv_leave)
        self.tv.bind("<Button-3>", self._on_tv_menu)
        self._tv_menu = tk.Menu(self.root, tearoff=0, bg=C_CARD, fg=C_TEXT,
                                activebackground=C_SEL_BG, activeforeground=C_TEXT,
                                bd=0, font=FONT_S)
        self._tv_menu.add_command(label="复制版本号", command=self._menu_copy_ver)
        self._tv_menu.add_command(label="复制下载链接", command=self._menu_copy_url)
        self._tv_menu.add_separator()
        self._tv_menu.add_command(label="打开官网支持页", command=self._on_open_site)

        # ---------- 空状态占位（三种语义：未选型号 / 查询中 / 无数据）
        self._empty_box = tk.Frame(self._list_holder, bg=C_CARD)
        # v2.3.0：上下各一个弹性占位，让空态内容在整块列表区里垂直居中。
        # 旧版靠 40px 的固定上边距贴顶；没有详情面板时右栏高 451px，
        # 内容会挂在上面三分之一处，下面空出近 280px。
        self._empty_pad_top = tk.Frame(self._empty_box, bg=C_CARD)
        self._empty_pad_top.pack(fill="both", expand=True)
        self._empty_icon = tk.Label(self._empty_box, text="◎",
                                    font=FONT_ICON,
                                    bg=C_CARD, fg=C_SKELETON)
        self._empty_icon.pack(pady=(S(0), S(6)))
        self._empty_box_lbl1 = tk.Label(self._empty_box, text="尚未选择主板型号",
                                        font=FONT_H, bg=C_CARD, fg=C_SUB)
        self._empty_box_lbl1.pack(pady=(S(0), S(8)))
        self._empty_box_lbl2 = tk.Label(self._empty_box,
                                        text="在中间「型号」栏点选一款主板，"
                                             "或直接在搜索框输入型号关键字。\n"
                                             "BIOS 列表将显示在这里，并提供下载与 SHA-256 校验。",
                                        font=FONT_S, bg=C_CARD, fg=C_DIM,
                                        justify="center")
        self._empty_box_lbl2.pack()
        self._empty_pad_bot = tk.Frame(self._empty_box, bg=C_CARD)
        self._empty_pad_bot.pack(fill="both", expand=True)
        self._show_empty(True)

        # ---------- 选中版本详情（v2.2.0 新增）
        # 定位分工：上方列表负责「扫」（版本 / 日期 / 大小 / 截断说明），
        # 这块负责「读」—— 旧版把整整 240px 高度空在列表下方（只有 4 个版本时
        # 占列表区 58%），而更新说明全文与 SHA-256 这些真正决定「要不要下、
        # 下完对不对」的信息，只能靠悬停或拉宽列才看得到。
        self._detail_sep = tk.Frame(rf, bg=C_HAIR, height=S(1))
        self._detail_sep.grid(row=3, column=0, sticky="ew", pady=(S(8), S(0)))
        # 用 tk.Frame + highlightthickness 做 1px 描边（与 _panel_note_box 同一惯用法）；
        # 底色取洼地 C_INSET，读作「嵌在卡片里的一块详情凹槽」，与表格体同族。
        self._detail_zone = tk.Frame(rf, bg=C_INSET,
                                     highlightbackground=C_BORDER,
                                     highlightthickness=S(1))
        self._detail_zone.grid(row=4, column=0, sticky="nsew", pady=(S(8), S(0)))
        self._detail_zone.columnconfigure(0, weight=1)
        self._detail_zone.rowconfigure(0, weight=1)
        dbox = tk.Frame(self._detail_zone, bg=C_INSET)
        dbox.grid(row=0, column=0, sticky="nsew", padx=S(10), pady=S(8))
        dbox.columnconfigure(0, weight=1)
        dbox.rowconfigure(0, weight=1)
        self._detail_box = dbox
        self._det_item = None          # 详情面板当前展示的 BIOS 条目

        # --- 内容态（选中某个版本时显示）
        det = tk.Frame(dbox, bg=C_INSET)
        det.grid(row=0, column=0, sticky="nsew")
        det.columnconfigure(0, weight=1)
        det.rowconfigure(1, weight=1)
        self._det_body = det
        self._det_desc_full = ""       # 更新说明全文（面板里只显示裁剪后的 N 行）
        d0 = tk.Frame(det, bg=C_INSET)
        d0.grid(row=0, column=0, sticky="ew")
        d0.columnconfigure(1, weight=1)
        self.v_det_title = tk.StringVar(value="")
        tk.Label(d0, textvariable=self.v_det_title, font=FONT_H, bg=C_INSET,
                 fg=C_TEXT, anchor="w").grid(row=0, column=0, sticky="w")
        self.v_det_meta = tk.StringVar(value="")
        tk.Label(d0, textvariable=self.v_det_meta, font=FONT_SS, bg=C_INSET,
                 fg=C_SUB, anchor="w").grid(row=0, column=1, sticky="w",
                                            padx=(S(10), S(0)))
        # 上下文动作放右侧：红主按钮留在底部工具条（全局动作）。
        # v2.3.0：文字色从 C_ACCENT_HOT 改回 C_TEXT —— 旧版「灰底 + 红字」与
        # 底栏红主按钮抢同一份注意力，两个入口都像主操作。现在红色只属于底栏。
        self.btn_det_dl = tk.Button(d0, text="下载此版本", font=FONT_S,
                                    bg=C_BTN, fg=C_TEXT,
                                    activebackground=C_BTN_HOVER,
                                    activeforeground=C_TEXT,
                                    relief="solid", bd=S(1), cursor="hand2",
                                    padx=S(12), pady=S(4),
                                    command=self._on_download)
        self.btn_det_dl.grid(row=0, column=3, sticky="e")
        self._bind_hover(self.btn_det_dl, C_BTN, C_BTN_HOVER)
        self.btn_det_copy = tk.Button(d0, text="复制下载链接", font=FONT_SS,
                                      bg=C_INSET, fg=C_BLUE,
                                      activebackground=C_INSET,
                                      activeforeground=C_ACCENT_HOT,
                                      relief="flat", bd=0, cursor="hand2",
                                      padx=S(8), pady=S(2),
                                      command=self._menu_copy_url)
        self.btn_det_copy.grid(row=0, column=2, sticky="e", padx=(S(0), S(8)))
        self._bind_hover_fg(self.btn_det_copy, C_BLUE, C_ACCENT_HOT)
        self.v_det_desc = tk.StringVar(value="")
        self._det_desc = tk.Label(det, textvariable=self.v_det_desc, font=FONT_S,
                                  bg=C_INSET, fg=C_SUB, anchor="nw",
                                  justify="left", wraplength=S(430))
        self._det_desc.grid(row=1, column=0, sticky="nsew", pady=(S(6), S(0)))
        # 换行宽跟随**详情面板**（不是标签自己）的宽度 —— 见 _on_detail_configure
        # 的注释：绑在标签上会形成「宽度↔换行高」的布局反馈环。
        self._detail_zone.bind("<Configure>", self._on_detail_configure, add="+")
        # 说明被裁到 DETAIL_MAX_LINES 行时，悬停看全文（与目录树同一套提示浮窗）
        self._det_desc.bind(
            "<Enter>", lambda e: self._show_tip(
                self._det_desc_full, e.x_root, e.y_root) if self._det_desc_full else None,
            add="+")
        self._det_desc.bind("<Leave>", lambda _e: self._hide_tip(), add="+")
        self.v_det_sha = tk.StringVar(value="")
        # v2.3.0：整串 SHA-256 实测需要 559px，面板只有 517px → 旧版被硬裁掉
        # 尾巴（64 位十六进制看不到最后几位，校验时等于没用），且裁切处没有
        # 省略号，看起来像"值就这么长"。改为中段省略 + 点击复制完整值。
        self._det_sha_full = ""
        self._det_sha_lbl = tk.Label(det, textvariable=self.v_det_sha, font=FONT_SS,
                                     bg=C_INSET, fg=C_DIM, anchor="w",
                                     cursor="hand2")
        self._det_sha_lbl.grid(row=2, column=0, sticky="w", pady=(S(6), S(0)))
        self._det_sha_lbl.bind("<Button-1>", lambda _e: self._copy_det_sha())
        self._bind_hover_fg(self._det_sha_lbl, C_DIM, C_BLUE)
        # --- 空态（未选型号 / 查询中 / 该型号无数据 / 尚未选版本）
        self._det_empty = tk.Label(dbox, text="", font=FONT_S, bg=C_INSET,
                                   fg=C_DIM, anchor="nw", justify="left",
                                   wraplength=S(430))
        self._det_empty.grid(row=0, column=0, sticky="nsew")
        self._show_detail(None)

        # 结果横幅（v2.4.0）：下载完成 / 失败的即时反馈，不必等弹窗
        # v2.0.0 修复：右栏 rf 的子控件全部走 grid，旧版这里用 pack 显示，
        # TclError 被 except 吞掉 —— 横幅其实从未真正显示过。
        # v2.2.0：原 row=7 的「下载区三行」已移出 rf，横幅随之上移到 row=5。
        self._toast = tk.Frame(rf, bg=C_OK_BG,
                               highlightbackground=C_OK_BORDER,
                               highlightthickness=S(1))
        self._toast_lbl = tk.Label(self._toast, text="", font=FONT_S, bg=C_OK_BG,
                                   fg=C_OK, anchor="w", justify="left", wraplength=S(760))
        self._toast_lbl.pack(fill="x", padx=S(12), pady=S(7))

    # ------------------------------------------------------------- 辅助
    def _set_status(self, msg, kind="info"):
        """排队一条状态消息。kind 决定状态点颜色：info / run / ok / err / warn。"""
        self.q.put(("status", msg, kind))

    def _ui_status(self, msg, kind="info"):
        self._status_full = msg or ""
        self._refit_status()
        color = {"run": C_AMBER, "ok": C_OK, "err": C_ACCENT_HOT,
                 "warn": C_AMBER}.get(kind, C_DIM)
        try:
            self.dot_status.configure(fg=color)
        except Exception:
            pass

    # ------------------------------------------- 状态栏长文本防御（v2.0.3）
    def _status_avail(self):
        """状态栏左侧文字可用的像素宽（栏宽 − 圆点 − 右侧常驻 − 内边距）。"""
        try:
            w = self.lbl_status.master.winfo_width()
            if w <= 1:
                w = self.root.winfo_width()
        except Exception:
            w = 0
        used = S(40)                       # 状态栏左右内边距（padding 20 × 2）
        for c in (self.dot_status, self.lbl_stat_right):
            try:
                used += c.winfo_reqwidth()
            except Exception:
                pass
        return max(S(80), w - used - S(24))

    def _refit_status(self):
        """按可用宽把状态文本用省略号收尾；完整消息留在 _status_full，悬停可见。

        旧版把整条消息直接塞进 Label：实测「已下载完成 + 完整路径」能到 2249px
        （最小窗口状态栏才 1100px），右侧常驻的「数据源 · 版本」会被挤掉，
        用户既看不到完整消息也看不到数据源。加 _fitting_status 守卫是因为
        set() 会改变 Label 请求宽、再次触发 <Configure>，防自激。
        """
        if self._fitting_status:
            return
        self._fitting_status = True
        try:
            shown = self._ellipsize(self._status_full or "",
                                    self._status_avail(), self._fm_tree)
            if self.v_status.get() != shown:
                self.v_status.set(shown)
        except Exception:
            pass
        finally:
            self._fitting_status = False

    def _status_tip(self, ev):
        """状态文本被省略时，悬停看完整消息。"""
        try:
            if self._status_full and self._status_full != self.v_status.get():
                self._show_tip(self._status_full, ev.x_root, ev.y_root, wrap=S(520))
        except Exception:
            pass

    def _on_note_box_configure(self, _ev=None):
        """主板级提示框宽度变化 → 换行宽跟随（v2.0.3）。

        旧版 wraplength 写死 S(720)：窗口拉宽到提示框 956px 时文本仍在 720 处
        换行（右侧空一整条，像没排满），窗口压窄到 656px 时又按 720 排版，
        句子尾部被裁掉。只在宽度真的变了才写回，避免「换行宽变 → 高度变 →
        Configure 再来」的自激循环。
        """
        try:
            w = self._panel_note_box.winfo_width()
            if w <= S(60):
                return
            want = max(S(120), w - 2 * S(10) - 2 * S(1))
            if str(self._panel_note_lbl.cget("wraplength")) != str(want):
                self._panel_note_lbl.configure(wraplength=want)
        except Exception as _e:
            _log("提示框换行宽自适应失败: %r" % (_e,))

    # ------------------------------------------------------------- 动效原语
    def _bind_hover(self, btn, base, hot, dur=None):
        """tk.Button 背景 hover 渐变。

        tk.Button 的 activebackground 只在按下瞬间硬切，这里额外用
        <Enter>/<Leave> + 帧动画做 140ms 缓出过渡，手感更连续。
        """
        dur = ANIM_HOVER if dur is None else dur
        key = ("hb", id(btn))

        def _to(target):
            try:
                if str(btn.cget("state")) == "disabled":
                    return
                cur = btn.cget("bg")
            except Exception:
                return
            self.anim.run(
                dur,
                lambda p: btn.configure(bg=_lerp_color(cur, target, _ease_out(p))),
                key=key)

        btn.bind("<Enter>", lambda e: _to(hot), add="+")
        btn.bind("<Leave>", lambda e: _to(base), add="+")

    def _bind_hover_fg(self, lbl, base, hot, dur=None):
        """tk.Label（当链接用）前景色 hover 渐变。"""
        dur = ANIM_HOVER if dur is None else dur
        key = ("hf", id(lbl))

        def _to(target):
            try:
                cur = lbl.cget("fg")
            except Exception:
                return
            self.anim.run(
                dur,
                lambda p: lbl.configure(fg=_lerp_color(cur, target, _ease_out(p))),
                key=key)

        lbl.bind("<Enter>", lambda e: _to(hot), add="+")
        lbl.bind("<Leave>", lambda e: _to(base), add="+")

    def _apply_row_colors(self, p):
        """按淡入进度 p 刷新列表行 tag 配色（普通 / 最新 / 两种 hover 变体）。"""
        e = _ease_out(p)
        try:
            self.tv.tag_configure("row", foreground=_lerp_color(C_CARD, C_TEXT, e))
            self.tv.tag_configure("latest",
                                  background=_lerp_color(C_CARD, C_GREEN_BG, e),
                                  foreground=_lerp_color(C_CARD, C_GREEN, e))
            self.tv.tag_configure("rowh",
                                  background=_lerp_color(C_CARD, C_ROW_HOVER, e),
                                  foreground=_lerp_color(C_CARD, C_TEXT, e))
            self.tv.tag_configure("latest_h",
                                  background=_lerp_color(C_CARD, C_LATEST_HOVER, e),
                                  foreground=_lerp_color(C_CARD, C_GREEN, e))
        except Exception:
            pass

    def _fade_list_in(self):
        """切换型号后 BIOS 列表淡入：前景/背景由卡片底色渐显到目标色。"""
        self._apply_row_colors(0.0)
        self.anim.run(ANIM_FADE, self._apply_row_colors, key="listfade")

    def _pb_set(self, target, dur=None):
        """进度条缓动补间：从当前显示值平滑逼近目标值，消除整数跳变。"""
        dur = ANIM_PB if dur is None else dur
        try:
            cur = float(self.pb["value"])
        except Exception:
            cur = 0.0
        target = max(0.0, min(100.0, float(target)))
        self.anim.run(
            dur,
            lambda p: self.pb.configure(value=cur + (target - cur) * _ease_out(p)),
            key="pb")

    def _pb_flash(self, c_from, c_to, dur=None):
        """进度条配色脉冲（下载完成时红 → 绿）。"""
        dur = ANIM_FLASH if dur is None else dur

        def _fr(p):
            col = _lerp_color(c_from, c_to, _ease_out(p))
            self.st.configure("TProgressbar", background=col, lightcolor=col)

        self.anim.run(dur, _fr, key="pbf")

    def _pb_color_reset(self):
        try:
            self.st.configure("TProgressbar", background=C_ACCENT,
                              lightcolor=C_ACCENT_HOT, darkcolor=C_ACCENT_DEEP)
        except Exception:
            pass

    def _pb_idle(self):
        """回到空闲态：清空进度条并把它从布局里收起。

        v2.3.0：旧版这条 12px 的空槽常驻（`mapped=1`），横贯整个工具条 ——
        没有下载任务时它读作「0% 卡住了」。收起后同一行由 lbl_pb 撑着（高度
        从 12 变 19，差 7px，不足以让下面的表格跳一下）。
        """
        try:
            self.anim.cancel("pb")
            self.pb["value"] = 0
            self.v_pb.set("")
            if self.pb.winfo_manager():
                self.pb.pack_forget()
            # 整行一起收起：只藏进度条的话，那 25px 会变成按钮下方一条莫名其妙的
            # 空白带（实测截图里就是"下半截空着"）。收起后底栏变矮、表格变高。
            if self._prow.winfo_manager():
                self._prow.grid_remove()
        except Exception as _e:
            _log("进度条复位失败: %r" % (_e,))

    def _pb_show(self):
        """把进度条放回「速度/剩余时间」标签的左边（pack 顺序要显式指定）。"""
        try:
            if not self._prow.winfo_manager():
                self._prow.grid()
            if not self.pb.winfo_manager():
                self.pb.pack(fill="x", side="left", expand=True,
                             before=self.lbl_pb)
        except Exception as _e:
            _log("进度条上屏失败: %r" % (_e,))

    def _status_flash(self, color, times=3):
        """状态栏文字闪烁（完成 / 失败时的注意力提示），结束回到常态色。"""
        def _fr(p):
            k = abs(((p * times * 2.0) % 2.0) - 1.0)   # 0→1→0 往复
            self.lbl_status.configure(fg=_lerp_color(color, C_STATUS_FG, k))

        self.anim.run(times * 300, _fr, key="statusflash",
                      on_done=lambda: self.lbl_status.configure(fg=C_STATUS_FG))

    def _shake_search(self):
        """搜索无结果时输入框左右轻抖（衰减 3 个来回）。"""
        def _fr(p):
            off = int(round(S(6) * (1.0 - p) * (1 if int(p * 6) % 2 == 0 else -1)))
            try:
                self._search_wrap.grid_configure(padx=(max(0, off), 0))
            except Exception:
                pass

        self.anim.run(ANIM_SHAKE, _fr, key="shake",
                      on_done=lambda: self._search_wrap.grid_configure(padx=(0, 0)))

    def _toast_show(self, text, ok=True, ttl=7000):
        """底部结果横幅：下载结果的即时反馈（自动隐藏，不必等弹窗）。"""
        bg = C_OK_BG if ok else C_RED_BG
        bd = C_OK_BORDER if ok else "#7a2a2e"
        fg = C_OK if ok else "#ff9b95"
        try:
            self._toast.configure(bg=bg, highlightbackground=bd)
            self._toast_lbl.configure(text=text, bg=bg, fg=fg)
            if not self._toast.winfo_manager():
                self._toast.grid(row=5, column=0, sticky="ew", pady=(S(8), S(0)))
        except Exception:
            return
        if self._toast_job:
            try:
                self.root.after_cancel(self._toast_job)
            except Exception:
                pass
        self._toast_job = self.root.after(ttl, self._toast_hide)

    def _toast_hide(self):
        self._toast_job = None
        try:
            if self._toast.winfo_manager():
                self._toast.grid_remove()
        except Exception:
            pass

    # ------------------------------------------------------------- 查询中加载态
    def _start_loading(self, title, sub="正在从华硕官网读取 BIOS 列表…"):
        self._loading_name = title
        self._empty_box_lbl1.configure(text=title, fg=C_SUB)
        self._empty_box_lbl2.configure(text=sub)
        self._show_empty(True, keep_anim=True)
        self._loading_ticks = 0
        if self._loading_job is None:
            self._loading_job = self.root.after(320, self._loading_step)

    def _loading_step(self):
        self._loading_job = None
        if self._loading_name is None or not self._empty_box.winfo_manager():
            return
        self._loading_ticks = (self._loading_ticks + 1) % 4
        try:
            self._empty_box_lbl1.configure(
                text="正在查询 %s%s" % (self._loading_name, "·" * self._loading_ticks))
            self._empty_icon.configure(
                fg=(C_ACCENT_DEEP, C_ACCENT, C_ACCENT_HOT, C_ACCENT)[self._loading_ticks])
        except Exception:
            return
        self._loading_job = self.root.after(320, self._loading_step)

    def _stop_loading(self):
        self._loading_name = None
        if self._loading_job is not None:
            try:
                self.root.after_cancel(self._loading_job)
            except Exception:
                pass
            self._loading_job = None
        try:
            self._empty_icon.configure(fg=C_SKELETON)
        except Exception:
            pass

    def _show_empty(self, on, title=None, sub=None, keep_title=False, keep_anim=False):
        """BIOS 列表为空时显示占位提示。

        切换发生在固定宿主容器 _list_holder 内部 —— 位置永远稳定
        （旧实现直接 pack/forget Treeview，重新 pack 会被追加到顺序末尾，
        列表会掉到下载区下方）。

        keep_title=True：型号已选定但无可用数据，占位显示型号名与原因，
        不重置标题区 / cur_slug（保留「打开官网支持页」能力）。
        keep_anim=True：查询中加载态，只切显示，不改任何文案。
        """
        if on:
            if self.tv.winfo_manager():
                self.tv.pack_forget()
            if not self._empty_box.winfo_manager():
                self._empty_box.pack(fill="both", expand=True)
            # 只丢弃残留的旧 status 消息（避免占位后被旧状态覆盖）；
            # busy/board/dl 等关键消息放回队列，绝不丢失
            kept = []
            try:
                while True:
                    m = self.q.get_nowait()
                    if m[0] != "status":
                        kept.append(m)
            except queue.Empty:
                pass
            for m in kept:
                self.q.put(m)
            if keep_anim:
                # v2.2.0：查询中 → 详情面板也让位给加载提示（旧版这里直接 return）
                self._show_detail(None, "正在查询该型号的 BIOS 版本…")
                return
            self._stop_loading()
            if keep_title:
                self._empty_icon.configure(fg=C_SKELETON)
                self._empty_box_lbl1.configure(text=title or "该型号暂无可用数据", fg=C_SUB)
                self._empty_box_lbl2.configure(text=sub or "")
                # v2.2.0：列表无数据 → 详情面板同步给出同一原因
                self._show_detail(None, sub or title or "该型号暂无可用 BIOS 数据")
            else:
                self._empty_icon.configure(fg=C_SKELETON)
                self._empty_box_lbl1.configure(text="尚未选择主板型号", fg=C_SUB)
                self._empty_box_lbl2.configure(
                    text="在中间「型号」栏中点选一款主板，"
                         "或直接在搜索框输入型号关键字。\n"
                         "BIOS 列表将显示在这里，并提供下载与 SHA-256 校验。")
                # 同时复位主板标题区，保持与空状态一致
                self._set_board_title("BIOS 明细")
                self.v_board_sub.set("")
                self.lbl_host.configure(text="")
                self.v_stat_right.set("数据源：—   ·   %s" % _ver_suffix())
                self.cur_slug = None
                self.cur_items = []
                self._latest_iids = set()
                self._show_detail(None)
                self._ui_status("就绪", "info")
        else:
            self._stop_loading()
            if self._empty_box.winfo_manager():
                self._empty_box.pack_forget()
            if not self.tv.winfo_manager():
                self.tv.pack(fill="both", expand=True)

    def _busy(self, on):
        """查询 / 下载中禁用主按钮（tk.Button 用 config(state=...) 切换）。"""
        # 先掐掉进行中的 hover 渐变：否则动画帧会把 disabled 底色覆盖回去
        self.anim.cancel(("hb", id(self.btn_dl)))
        if on:
            self.btn_dl.config(state=tk.DISABLED, bg=C_ACCENT_BTN_DIS,
                               disabledforeground=C_ACCENT_BTN_FG_DIS)
        else:
            self.btn_dl.config(state=tk.NORMAL, bg=C_ACCENT_BTN,
                               disabledforeground=C_ACCENT_BTN_FG_DIS)

    def _set_downloading(self, on):
        """下载态切换：主按钮禁用 + 显示「取消下载」；结束后隐藏并还原。"""
        self._busy(on)
        try:
            if on:
                self.btn_cancel.config(state=tk.NORMAL, bg=C_BTN)
                if not self.btn_cancel.winfo_manager():
                    # v2.2.0：操作行搬到跨栏工具条，取消按钮在列 4（下载在列 5）
                    self.btn_cancel.grid(row=0, column=4, sticky="nsew",
                                         padx=(S(0), S(0)))
            elif self.btn_cancel.winfo_manager():
                self.btn_cancel.grid_remove()
        except Exception:
            pass

    # ------------------------------------------------------------- 搜索 / 快捷键
    def _search_value(self):
        """搜索框有效内容（占位文案显示中时视为空）。"""
        if self._search_ph:
            return ""
        return (self.v_search.get() or "").strip()

    def _search_focus_in(self, _ev=None):
        if self._search_ph:
            self._search_ph = False
            self.v_search.set("")
            try:
                self.ent_search.configure(fg=C_TEXT)
            except Exception:
                pass

    def _search_focus_out(self, _ev=None):
        if not (self.v_search.get() or "").strip():
            self._search_ph = True
            self.v_search.set(SEARCH_PH)
            try:
                self.ent_search.configure(fg=C_DIM)
            except Exception:
                pass

    def _on_search_changed(self, *_a):
        """输入即过滤（140ms 防抖，避免逐字符重建目录树）。"""
        if self._search_ph:
            return
        if self._search_job is not None:
            try:
                self.root.after_cancel(self._search_job)
            except Exception:
                pass
        self._search_job = self.root.after(140, self._apply_search)

    def _apply_search(self):
        self._search_job = None
        q = self._search_value().lower()
        if q == self._search_q:
            return
        self._search_q = q
        self._populate_tree()
        if q and self._search_hits == 0:
            self._shake_search()
            self._set_status(
                "目录中未找到「%s」—— 可点中栏「去官网搜索」查已停售型号"
                % self._search_value(), "warn")

    def _on_site_search(self):
        """目录无命中时的出路：跳到华硕官网站内搜索（覆盖已停售型号）。"""
        q = self._search_value()
        if q:
            webbrowser.open(BASE + "/search/?SearchKey=" + urllib.parse.quote(q))
        else:
            webbrowser.open(BASE + "/motherboards-components/motherboards/all-series/")

    def _bind_shortcuts(self):
        r = self.root

        def _focus_search(_e=None):
            self.ent_search.focus_set()
            self.ent_search.select_range(0, "end")
            return "break"

        def _refresh(_e=None):
            self._refresh_now(None)
            return "break"

        def _dl(_e=None):
            self._on_download()
            return "break"

        r.bind("<Control-f>", _focus_search)
        r.bind("<Control-F>", _focus_search)
        r.bind("<F5>", _refresh)
        r.bind("<Escape>", self._on_escape)
        self.tv.bind("<Return>", _dl)
        self.ent_search.bind("<Return>", self._on_search_enter)

    def _on_escape(self, _ev=None):
        """Esc：清空搜索并回到完整目录。"""
        if self._search_value():
            self._search_ph = False
            self.v_search.set("")
            try:
                self.ent_search.configure(fg=C_TEXT)
            except Exception:
                pass
            self._search_q = ""
            self._populate_tree()
            self.root.focus_set()
            self._set_status("已清空搜索，显示完整目录")
            return "break"
        return None

    def _on_search_enter(self, _ev=None):
        """搜索框回车：跳到第一个命中型号并加载 BIOS 列表。"""
        for p in self.cat_tree.get_children():
            for c in self.cat_tree.get_children(p):
                bs = self.cat_tree.get_children(c)
                if bs:
                    self.cat_tree.item(p, open=True)
                    self.cat_tree.item(c, open=True)
                    self.cat_tree.selection_set(bs[0])
                    self.cat_tree.see(bs[0])
                    self._on_tree_select()
                    return "break"
        return "break"

    # ------------------------------------------------------------- 列表 hover / 右键
    def _row_desc(self, iid):
        """该行「更新说明」的完整文本（换行折平）——供截断显示与悬停全文共用。"""
        it = self._iid_item.get(iid) or {}
        return (it.get("download_description") or "").replace("\n", " ").strip()

    def _on_tv_motion(self, ev):
        iid = self.tv.identify_row(ev.y)
        if iid != self._hover_row:
            self._set_hover(iid)
        # v2.0.3：悬停「更新说明」列且该行说明确实被列宽截断 → 浮窗显示完整说明。
        # 左栏型号名、右栏型号标题都已支持悬停看全名，列表里最长的一段文字
        # （实测 120 字只显示得下 28 字）反而没有出口 —— 而「这次 BIOS 改了什么」
        # 正是用户决定要不要升级的依据。
        try:
            if iid and self.tv.identify_column(ev.x) == "#4":
                desc = self._row_desc(iid)
                if desc and desc != str(self.tv.item(iid, "values")[3]):
                    self._show_tip(desc, ev.x_root, ev.y_root, wrap=S(460))
                    return
        except Exception:
            pass
        self._hide_tip()

    def _on_tv_leave(self, _ev=None):
        self._clear_hover()
        self._hide_tip()

    def _set_hover(self, iid):
        prev = self._hover_row
        if prev and prev != iid:
            try:
                self.tv.item(prev, tags=("latest", "row")
                             if prev in self._latest_iids else ("row",))
            except Exception:
                pass
        self._hover_row = iid or None
        if iid:
            try:
                self.tv.item(iid, tags=("latest_h",)
                             if iid in self._latest_iids else ("rowh",))
            except Exception:
                pass

    def _clear_hover(self):
        self._set_hover(None)

    def _on_tv_menu(self, ev):
        iid = self.tv.identify_row(ev.y)
        if not iid:
            return
        self.tv.selection_set(iid)
        self.tv.focus(iid)
        self._set_hover(iid)
        try:
            self._tv_menu.tk_popup(ev.x_root, ev.y_root)
        finally:
            try:
                self._tv_menu.grab_release()
            except Exception:
                pass

    def _clipboard(self, text):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text or "")
            self._set_status("已复制到剪贴板：%s" % (text or "")[:70], "ok")
        except Exception as e:
            _log("clipboard err %r" % (e,))

    def _menu_copy_ver(self):
        it = self._selected_item()
        if it:
            self._clipboard(clean_version(it.get("download_version")))

    def _menu_copy_url(self):
        it = self._selected_item()
        if it:
            self._clipboard(it.get("download_url") or "")

    def _wake(self):
        """主线程 put 后兜一手：确认消息泵在表上（后台线程不走这里，见 MsgQueue）。"""
        self._ensure_poll()

    def _spawn(self, fn, *args):
        """启动后台任务的**唯一入口**：登记在册 + 确保消息泵保持心跳。

        v2.0.2：两件事一起解决 ——
          · 线程与并发：所有后台线程从这里出生，在册数量可数、可断言；
          · 调度：泵只在「有后台任务在跑」时保持 160ms 心跳，任务全部结束就停表，
            不再有永续空转的定时器。任务期间后台线程只往队列里放消息（不碰 Tk），
            由心跳把消息取走。
        """
        with self._wlock:
            self._workers += 1

        def _run():
            try:
                fn(*args)
            except Exception as e:
                _log("后台任务异常 %r: %r" % (getattr(fn, "__name__", fn), e))
            finally:
                with self._wlock:
                    self._workers -= 1

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        self._ensure_poll()
        return t

    def _hot(self):
        """当前是否有后台任务在跑 —— 有则消息泵保持低频心跳，保证消息及时取走。"""
        with self._wlock:
            return self._workers > 0

    def _ensure_poll(self, _ev=None):
        """确保消息泵在表上（<<qmsg>> 事件与首次启动共用）。"""
        if self._polling:
            return
        self._polling = True
        self._poll_queue()

    def _poll_queue(self):
        """主线程消息泵：把后台线程的队列消息派发到 UI。

        v2.0.0：间隔自适应 —— 有消息（查询 / 下载中）时 60ms 保证进度顺滑，
        空闲时放宽到 160ms，减少无谓唤醒。
        v2.0.2：空闲**停表** —— 旧版无条件重排 after，等于每 160ms 醒一次空转，
        进程永远不真正空闲。现在只有「队列刚取到消息 / 还有后台任务在跑」才继续
        排表；主线程 put 消息会即时唤醒，后台任务在出生时（_spawn）就重新排表。
        收尾时再查一次队列，避免「刚判空 → 紧接着被 put」这条窄缝丢消息。
        """
        got = 0
        try:
            while True:
                m = self.q.get_nowait()
                self._dispatch(m)
                got += 1
        except queue.Empty:
            pass
        if got or self._hot() or not self.q.empty():
            self.root.after(60 if got else 160, self._poll_queue)
        else:
            self._polling = False
            if not self.q.empty():
                self._ensure_poll()

    def _apply_dark_titlebar(self, win=None):
        """Windows 10 2004+ / 11：让 DWM 标题栏跟随内容明暗。

        本工具是深色皮肤（TITLEBAR_DARK=True）→ 传 1 开启沉浸式深色标题栏，
        与内容浑然一体；若换成浅色皮肤必须传 0，否则会出现「深色标题栏 +
        浅色内容」的视觉割裂。

        v2.1.0：加 win 参数 —— 旧版只对主窗口生效，**设置窗的标题栏一直是浅色的**，
        弹出来就是一条白条压在一整片深色上，是这次焕新里最扎眼的不一致。
        已在设置窗创建后一并调用。
        """
        try:
            src = win if win is not None else self.root
            hwnd = ctypes.windll.user32.GetParent(src.winfo_id())
            if not hwnd:
                hwnd = src.winfo_id()
            # DWMWA_USE_IMMERSIVE_DARK_MODE：20 (Win10 2004+) / 19 (老 build)
            # 取值由 TITLEBAR_DARK 控制（本工具深色主题 = 1 开启）
            for attr in (20, 19):
                val = ctypes.c_int(1 if TITLEBAR_DARK else 0)
                ok = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(val), ctypes.sizeof(val))
                if ok == 0:  # S_OK
                    return
        except Exception:
            pass

    def _dispatch(self, m):
        kind = m[0]
        try:
            if kind == "status":
                self._ui_status(m[1], m[2] if len(m) > 2 else "info")
            elif kind == "busy":
                self._busy(m[1])
            elif kind == "cat_busy":
                self._cat_busy(m[1])
            elif kind == "cat_prog":
                self._cat_progress(m[1])
            elif kind == "catalog":
                self._apply_catalog(m[1], m[2])      # (catalog, err)
            elif kind == "board":
                self._apply_board(m[1])              # (slug,title,groups,note,err)
            elif kind == "dl_progress":
                self._dl_progress(*m[1])
            elif kind == "dl_done":
                self._dl_done(*m[1])
            elif kind == "err":
                messagebox.showerror(APP_TITLE, m[1], parent=self.root)
            elif kind == "info":
                messagebox.showinfo(APP_TITLE, m[1], parent=self.root)
        except Exception as e:
            _log("dispatch err %s: %r" % (kind, e))

    # ------------------------------------------------------------- 目录
    def _boot(self):
        cat, age = load_catalog_cached()
        if cat is not None:
            self.catalog = cat
            self.cat_updated = cat.get("updated", "")
            self._populate_tree()
            days = int(self.cfg.get("refresh_days") or 0)
            if days > 0 and age >= days * 86400:
                self._set_status("目录已过期（缓存 %d 天 > 设置的 %d 天），正在后台刷新…"
                                 % (age // 86400, days))
                self._spawn(self._catalog_worker)
            else:
                hrs = max(0, int(age // 3600))
                tip = "不自动刷新" if days <= 0 else "每 %d 天自动检查" % days
                self._set_status("目录已就绪（缓存 %d 小时 · %s）" % (hrs, tip))
        else:
            self._set_status("首次使用：正在从官网加载主板目录…")
            self._spawn(self._catalog_worker)

    # ------------------------------------------------------------- 设置（目录更新 / 显示范围）
    def _refresh_days_label(self, days):
        for d, lbl in REFRESH_OPTS:
            if d == days:
                return lbl
        return "7 天"

    def _open_settings(self):
        cfg = self.cfg
        # 平台候选（以当前目录为准；目录未就绪时用标准三平台）
        plat_names = []
        if self.catalog:
            plat_names = [b["name"] for b in self.catalog.get("brands") or []]
        if not plat_names:
            plat_names = ["Intel", "AMD", "其他"]
        # 芯片组候选：(展示文本, 芯片组名)；含所属平台与数量
        chip_opts = []
        if self.catalog:
            for b in self.catalog.get("brands") or []:
                for cs in b.get("chipsets") or []:
                    if cs.get("boards"):
                        chip_opts.append(("%s · %s（%d 款）" % (b["name"], cs["name"],
                                                                len(cs["boards"])), cs["name"]))
        cur_plat = cfg.get("show_platforms") or []
        cur_chip = cfg.get("chip_filter") or []
        cur_days = int(cfg.get("refresh_days") or 0)

        win = tk.Toplevel(self.root)
        win.title("设置")
        _sw, _sh = _fit_size(780, 700, 60)
        win.geometry("%dx%d" % (_sw, _sh))
        win.minsize(min(S(740), _sw), min(S(660), _sh))
        win.transient(self.root)
        win.grab_set()
        win.configure(bg=C_BG)
        # v2.1.0：设置窗一并套用沉浸式深色标题栏。
        # 延时必须够长：实测「创建后立刻设」标题栏仍是 #f3f3f3（浅色，未生效），
        # 「窗口映射后再设」才变 #202020（深色）。40ms 太早，与主窗口一样取 180ms。
        win.after(180, lambda w=win: self._apply_dark_titlebar(w))
        tk.Frame(win, bg=C_ACCENT, height=S(3)).pack(fill="x", side="top")
        frm = ttk.Frame(win, style="CardInner.TFrame", padding=S(16))
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="设置 — 目录更新与显示范围", style="H.TLabel").pack(anchor="w")
        ttk.Label(frm, text="保存后立即生效，无需重启。设置保存于程序目录 asus_bios_checker.cfg。",
                  style="Sub.TLabel").pack(anchor="w", pady=(S(2), S(6)))

        def _sec(parent, title):
            box = tk.Frame(parent, bg=C_INSET,
                           highlightbackground=C_BORDER, highlightthickness=S(1))
            box.pack(fill="x", pady=(S(6), S(0)))
            head = tk.Frame(box, bg=C_INSET)
            head.pack(fill="x")
            # v2.1.0：区块标题字色 C_ACCENT_DEEP(#b81e1e)→C_ACCENT_HOT(#ff6a4d)。
            # 旧色在洼地底（#0d1015）上的对比度只有约 2.5:1，本就是可读性欠债；
            # 换亮红后约 6.7:1，同时与主界面的强调色统一。
            tk.Label(head, text=title, font=FONT_B, bg=C_INSET, fg=C_ACCENT_HOT,
                     anchor="w").pack(side="left", padx=S(12), pady=(S(8), S(0)))
            inner = tk.Frame(box, bg=C_INSET)
            inner.pack(fill="x", padx=S(12), pady=(S(4), S(10)))
            return inner

        # ---------- ① 目录自动刷新间隔（检测频率）
        inner1 = _sec(frm, "① 更新检测 — 目录自动刷新")
        tk.Label(inner1, text="目录自动刷新间隔：", font=FONT, bg=C_INSET,
                 fg=C_TEXT).grid(row=0, column=0, sticky="w")
        v_refresh = tk.StringVar(value=self._refresh_days_label(cur_days))
        ttk.Combobox(inner1, textvariable=v_refresh, state="readonly", width=12,
                     values=[lbl for _d, lbl in REFRESH_OPTS], font=FONT
                     ).grid(row=0, column=1, sticky="w", padx=(S(8), S(0)), ipady=S(2))
        tk.Button(inner1, text="立即更新目录", font=FONT_ACCENT, bg=C_ACCENT_BTN, fg="#ffffff",
                  activebackground=C_ACCENT_BTN_HOT, activeforeground="#ffffff",
                  relief="flat", bd=0, padx=S(12), pady=S(3), cursor="hand2",
                  command=lambda: self._refresh_now(win)
                  ).grid(row=0, column=2, sticky="w", padx=(S(16), S(0)))
        tk.Label(inner1, text="达到间隔后，启动程序时会在后台自动从官网刷新主板目录（新版芯片组 / 新型号自动出现）；\n"
                              "选「不自动刷新」则只在点「立即更新目录」时联网更新。离线时更新失败不影响浏览已有型号。",
                 font=FONT_SS, bg=C_INSET, fg=C_SUB, justify="left"
                 ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(S(4), S(0)))

        # ---------- ② 目录显示范围：平台
        inner2 = _sec(frm, "② 目录显示范围 — 平台")
        plat_vars = {}
        for i, pn in enumerate(plat_names):
            var = tk.BooleanVar(value=(not cur_plat) or (pn in cur_plat))
            plat_vars[pn] = var
            tk.Checkbutton(inner2, text=pn + " 主板", variable=var, font=FONT,
                           bg=C_INSET, fg=C_TEXT, activebackground=C_INSET,
                           activeforeground=C_TEXT, selectcolor=C_CARD,
                           highlightthickness=0, bd=0
                           ).grid(row=0, column=i, sticky="w", padx=(S(0), S(26)))

        # ---------- ③ 目录显示范围：芯片组
        inner3 = _sec(frm, "③ 目录显示范围 — 芯片组")
        v_mode = tk.IntVar(value=0 if not cur_chip else 1)
        tk.Radiobutton(inner3, text="显示全部芯片组", variable=v_mode, value=0,
                       font=FONT, bg=C_INSET, fg=C_TEXT, activebackground=C_INSET,
                       activeforeground=C_TEXT, selectcolor=C_CARD,
                       highlightthickness=0, bd=0).grid(row=0, column=0, sticky="w")
        tk.Radiobutton(inner3, text="仅显示以下勾选的芯片组：", variable=v_mode, value=1,
                       font=FONT, bg=C_INSET, fg=C_TEXT, activebackground=C_INSET,
                       activeforeground=C_TEXT, selectcolor=C_CARD,
                       highlightthickness=0, bd=0).grid(row=0, column=1, sticky="w", padx=(S(20), S(0)))
        if chip_opts:
            box3 = tk.Frame(inner3, bg=C_INSET)
            box3.grid(row=1, column=0, columnspan=3, sticky="nsew", pady=(S(6), S(0)))
            lb = tk.Listbox(box3, selectmode="multiple", exportselection=False,
                            font=FONT_S, width=58, height=min(9, max(4, len(chip_opts))),
                            bg=C_CARD, fg=C_TEXT, relief="flat", borderwidth=0,
                            highlightthickness=S(1), highlightbackground=C_BORDER,
                            selectbackground=C_SEL_BG, selectforeground=C_TEXT,
                            activestyle="none")
            lb.pack(side="left", fill="both", expand=True)
            sb3 = ttk.Scrollbar(box3, orient="vertical", command=lb.yview)
            sb3.pack(side="right", fill="y")
            lb.configure(yscrollcommand=sb3.set)
            for _disp, _name in chip_opts:
                lb.insert("end", _disp)
            for i, (_disp, _name) in enumerate(chip_opts):
                if _name in cur_chip:
                    lb.selection_set(i)
            btn_col = tk.Frame(box3, bg=C_INSET)
            btn_col.pack(side="right", fill="y", padx=(S(8), S(0)))

            def _sel_all():
                lb.selection_set(0, "end")

            def _sel_none():
                lb.selection_clear(0, "end")

            def _sel_toggle():
                cur = set(lb.curselection())
                lb.selection_clear(0, "end")
                for i in range(lb.size()):
                    if i not in cur:
                        lb.selection_set(i)
            for text, fn in (("全选", _sel_all), ("全不选", _sel_none), ("反选", _sel_toggle)):
                ttk.Button(btn_col, text=text, width=7,
                           command=fn).pack(pady=(S(0), S(6)), anchor="e")
        else:
            tk.Label(inner3, text="（目录尚未加载，加载后可在此勾选芯片组范围）",
                     font=FONT_S, bg=C_INSET, fg=C_SUB
                     ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(S(6), S(0)))

        # ---------- 按钮
        bar = ttk.Frame(frm, style="CardInner.TFrame")
        bar.pack(fill="x", pady=(S(10), S(0)))
        ttk.Button(bar, text="恢复默认", command=lambda: self._settings_defaults(
            v_refresh, plat_vars, v_mode, lb if chip_opts else None)).pack(side="left")
        ttk.Button(bar, text="取消", command=win.destroy).pack(side="right")
        ttk.Button(bar, text="保存", style="Accent.TButton",
                   command=lambda: self._settings_apply(
                       win, v_refresh, plat_vars, plat_names, v_mode,
                       lb if chip_opts else None, chip_opts)).pack(side="right", padx=(S(0), S(8)))
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        self.root.wait_window(win)

    def _settings_defaults(self, v_refresh, plat_vars, v_mode, lb):
        """恢复默认：仅把控件复位，需再点「保存」生效。"""
        v_refresh.set(self._refresh_days_label(DEFAULT_REFRESH_DAYS))
        for var in plat_vars.values():
            var.set(True)
        v_mode.set(0)
        if lb is not None:
            lb.selection_clear(0, "end")

    def _settings_apply(self, win, v_refresh, plat_vars, plat_names, v_mode, lb, chip_opts):
        days = DEFAULT_REFRESH_DAYS
        for d, lbl in REFRESH_OPTS:
            if lbl == v_refresh.get():
                days = d
                break
        plats = [pn for pn, var in plat_vars.items() if var.get()]
        if not plats:
            messagebox.showwarning(APP_TITLE, "至少勾选一个平台，否则目录将没有可显示内容。",
                                   parent=win)
            return
        if v_mode.get() == 1 and chip_opts is None:
            # QA 复审修复：目录尚未加载时没有芯片组清单可选。旧版落到下面的 else，
            # 把「仅显示勾选」静默存成 None（= 显示全部），用户意图被吞掉。
            # 改为保留原有过滤设置不动。
            chip_sel = self.cfg.get("chip_filter")
        elif v_mode.get() == 1:
            idxs = lb.curselection()
            if not idxs:
                messagebox.showwarning(APP_TITLE, "已选择「仅显示勾选的芯片组」，请至少勾选一项。",
                                       parent=win)
                return
            chip_sel = [chip_opts[i][1] for i in idxs]
        else:
            chip_sel = None
        # 全平台勾选 -> 存 None（等价于默认，语义更干净）
        plat_store = plats if len(plats) < len(plat_names) else None
        old_days = int(self.cfg.get("refresh_days") or 0)
        self.cfg = {
            "refresh_days": days,
            "show_platforms": plat_store,
            "chip_filter": chip_sel,
            # v2.0.2：save_dir 是「下载」页签之外的持久状态，重建 cfg 时必须带上，
            # 否则点一次「应用」就把记住的保存目录抹掉了。
            "save_dir": self.cfg.get("save_dir"),
        }
        # v2.0.2：保存失败要如实上报 —— 旧版忽略 save_cfg 返回值，无论存没存住
        # 都提示「设置已保存并应用」，用户以为存住了，重启才发现全丢。
        if not save_cfg(self.cfg):
            win.destroy()
            self._populate_tree()
            self._set_status("设置未能写入磁盘（配置文件不可写），本次改动只在本次运行内有效。",
                             "err")
            return
        win.destroy()
        self._populate_tree()
        # 间隔收严且当前缓存已超期 -> 立即后台刷新（离线失败无碍浏览）
        _c, age = load_catalog_cached()
        if days > 0 and old_days != days and age >= days * 86400:
            self._set_status("刷新间隔已设为 %d 天：当前缓存已超期，正在后台刷新目录…" % days)
            self._spawn(self._catalog_worker)
        else:
            self._set_status("设置已保存并应用（目录自动刷新：%s）" % v_refresh.get())

    def _refresh_now(self, win=None):
        """设置面板 / 左栏 ↻：「立即更新目录」。

        v2.0.2 重入保护：旧版每点一次就起一个后台线程，连点 5 次 = 5 个全量拉取
        同时在跑（每个约 65 次 HTTP 请求）——既拖慢自己，也可能被官网限流。
        现在用 _cat_running 标志 + 按钮禁用挡住重复触发，线程 finally 里复位。
        """
        try:
            if win is not None and win.winfo_exists():
                win.destroy()
        except Exception:
            pass
        if self._cat_running:
            self._set_status("目录刷新已在进行中，请稍候…", "warn")
            return False
        self._cancel_cat_retry()        # v2.3.1：手动/自动重试启动前撤销排队任务
        self._cat_running = True
        try:
            self.btn_cat_refresh.configure(state=tk.DISABLED)
        except Exception:
            pass
        self._set_status("正在从官网刷新主板目录…")
        self._spawn(self._catalog_worker)
        return True

    def _cat_busy(self, on):
        """目录刷新在途状态复位（主线程调用）。"""
        self._cat_running = bool(on)
        try:
            self.btn_cat_refresh.configure(state=tk.DISABLED if on else tk.NORMAL)
        except Exception:
            pass

    def _catalog_worker(self):
        def prog(msg):
            self.q.put(("cat_prog", msg))
        try:
            api = self.api
            cat = fetch_catalog(api, progress=prog)
            save_catalog(cat)
            self.q.put(("catalog", cat, None))
        except Exception as e:
            self.q.put(("catalog", None, str(e)[:300]))
        finally:
            # v2.0.2：无论成功失败都要复位重入标志与按钮，否则 ↻ 会永久禁用
            self.q.put(("cat_busy", False))
            self.q.put(("busy", False))

    def _cat_progress(self, msg):
        self._ui_status("目录加载中… %s" % msg)

    def _apply_catalog(self, cat, err):
        if cat is None:
            self.v_cat.set("目录加载失败")
            self._cat_failed(err)
            return
        self.catalog = cat
        self.cat_updated = cat.get("updated", "")
        self._cat_retry_n = 0
        self._cancel_cat_retry()
        self._populate_tree()
        self._set_status("目录就绪：%d 款主板（%s 更新）" % (cat.get("total", 0), self.cat_updated))

    # ---------------------------------------------- 目录更新失败的收尾（v2.3.1）
    def _cat_failed(self, err):
        """目录更新失败：说人话 + 交代缓存情况 + 排一次自动重试。

        旧版只把异常原样贴进状态栏、然后就此作罢。而这类失败绝大多数是
        路由器重连 / 运营商 DNS 抖动造成的十几秒窗口 —— 自动重试一次就过去了。
        """
        msg = err or "网络请求失败"
        _log("目录更新失败: %s" % msg)
        total = int((self.catalog or {}).get("total") or 0)
        if total:
            head = "目录更新失败：%s\n本地缓存的 %d 款型号可继续使用。" % (msg, total)
        else:
            head = "目录更新失败：%s\n首次使用需要联网建立目录。" % msg
        delay = self._schedule_cat_retry()
        if delay:
            tail = "%d 秒后自动重试（第 %d/%d 次）" % (
                delay, self._cat_retry_n, len(CAT_RETRY_DELAYS))
        else:
            tail = "已自动重试 %d 次仍未成功，请检查网络后点左栏 ↻ 重试。" % len(CAT_RETRY_DELAYS)
        self._set_status(head + "\n" + tail, "err")

    def _schedule_cat_retry(self):
        """排下一次自动重试，返回等待秒数；已达上限或已有待执行任务则返回 0。"""
        if self._cat_retry_id is not None or self._cat_retry_n >= len(CAT_RETRY_DELAYS):
            return 0
        delay = CAT_RETRY_DELAYS[self._cat_retry_n]
        self._cat_retry_n += 1
        try:
            self._cat_retry_id = self.root.after(delay * 1000, self._cat_retry_fire)
        except Exception:
            self._cat_retry_id = None
            return 0
        return delay

    def _cat_retry_fire(self):
        self._cat_retry_id = None
        if self._cat_running:       # 用户已经手动点过 ↻，别叠加
            return
        self._refresh_now()

    def _cancel_cat_retry(self):
        if self._cat_retry_id is None:
            return
        try:
            self.root.after_cancel(self._cat_retry_id)
        except Exception:
            pass
        self._cat_retry_id = None

    # ------------------------------------------------- 目录树列宽自适应（v2.1.1）
    def _px(self, text, fm):
        """用指定具名字体实测文本像素宽（带缓存，DPI 变化自动失效）。

        v2.0.2：缓存加了上界（PX_CACHE_MAX）—— 目录全量换一批就会多出几千个
        新键，无上界的缓存只是「很慢的泄漏」。超限整体丢弃，重建代价很低。
        """
        if self._px_dpi != _DPI:
            self._px_cache = {}
            self._px_dpi = _DPI
        key = (fm, text)
        v = self._px_cache.get(key)
        if v is None:
            v = 0
            if fm:
                try:
                    v = int(self.root.tk.call("font", "measure", fm, text))
                except Exception:
                    v = 0
            if v <= 0:      # 字体不可用时的兜底估算（100% 下：半角 9px / 全角 15px）
                v = sum(S(9) if ord(ch) < 128 else S(15) for ch in text)
            if len(self._px_cache) >= PX_CACHE_MAX:
                self._px_cache.clear()
            self._px_cache[key] = v
        return v

    def _text_px(self, text):
        """目录树字体下的文本像素宽（列宽自适应用）。"""
        return self._px(text, self._fm_tree)

    def _ss_px(self, text):
        """次级小字（FONT_SS）下的文本像素宽（详情面板 SHA-256 用）。"""
        return self._px(text, self._fm_ss or self._fm_tree)

    def _chip_col_need(self):
        """实测左栏 #0 列所需宽度（平台名 / 芯片组名的最大值 + 前缀开销）。

        与旧版 _cat_col_need 的差别：**不再把型号名算进来** —— 型号层已搬到中栏，
        左栏只装「平台 + 芯片组」两级，宽度需求从 492px 骤降到 ~150px。
        仍基于全量目录计算（与搜索/过滤无关），避免输入搜索词时左栏宽度抖动。
        """
        cat = self.catalog or {}
        key = (_DPI, cat.get("updated"), cat.get("total"))
        if self._need_key == key:
            return self._need_v
        ind = S(CAT_INDENT)
        # 所有层级共用同一个「前缀开销」：内容区左内边距 + Tk 的展开箭头槽。
        # v2.0.4 修正：旧公式对有子节点的行额外 +16、对叶子只 +5 —— 与实际相反。
        # 箭头槽是**每个 item 都有的固定开销**，与是否有子节点无关（已实测）。
        lead = CAT_TREE_LEAD + CAT_IND_SLOT
        need = S(120)
        for b in cat.get("brands") or []:
            chips = b.get("chipsets") or []
            n_b = sum(len(cs.get("boards") or []) for cs in chips)
            need = max(need, self._text_px("%s 主板（%d 款）" % (b["name"], n_b)) + lead)
            for cs in chips:
                bs = cs.get("boards") or []
                need = max(need, ind + self._text_px(
                    "%s（%d 款）" % (cs["name"], len(bs))) + lead)
        # v2.0.5：再多留 CAT_COL_MARGIN px —— 否则「最长的那条」恰好等于列宽，
        # 落在「tk 判定放得下 vs 实际可绘制少 1px」的刀刃上（见 skill §14.9）。
        need += CAT_COL_MARGIN
        self._need_key, self._need_v = key, need
        return need

    def _body_width(self):
        """布局区可用宽度（尚未布局时按目标窗口尺寸估算）。"""
        w = self._body.winfo_width() if self._body else 0
        if w < S(200):
            w = _fit_size(1200, 780, 40)[0] - S(40)
        return w

    def _body_pane_pad(self):
        """布局区容器自身的左右内边距 + 边框（三张卡片之外被吃掉的那部分）。

        v2.0.5 修：`_body_width()` 返回的是**容器外框宽**，里面还有 `padding=(20,8,20,6)`
        （实测 38px）不属于任何一栏。旧代码算右栏保底时没扣这 38px，误差一直存在但
        从未生效（列宽被 300px 固定上限压着，右栏总是富余）；v2.0.5 放宽列宽后
        它立刻显形 —— 实测右栏被压到 594 < 保底 620（A23.5 红）。
        v2.2.0 只是把「两卡 + 一处间距」改成「三卡 + 两处间距」，口径不变：
        几何已定则**实测**（DPI 无关），未定则用 CAT_BODY_PAD 估算；三张卡片都
        拿到真实宽度后才缓存，避免把首次布局的估算值冻住。
        """
        if self._body_pad is None:
            try:
                cw = self._cf.winfo_width()
                mw = self._mf.winfo_width()
                rw = self._rf.winfo_width()
                if cw > 1 and mw > 1 and rw > 1:
                    self._body_pad = max(
                        0, self._body_width() - cw - mw - rw - S(PANE_GAP) * 2)
            except Exception:
                pass
            if self._body_pad is None:
                return S(CAT_BODY_PAD)      # 首次布局前的估算
        return self._body_pad

    def _layout_panes(self):
        """按窗口宽度分配三栏宽度，并把两棵树的 #0 列宽算好。

        预算：avail = 布局区内宽 − 2×栏间距
          固定：左栏 PICKER_CHIP_W(200) + 中栏 PICKER_MODEL_W(340)
          剩余：右栏（吃全部剩余，保底 PICKER_RIGHT_MIN=490）

        挤不下时按「先借中栏、再借左栏」的顺序让位 —— 优先保右栏（BIOS 明细是
        主工作面），其次保左栏（平台/芯片组名短，缩一点仍读得出），中栏的型号名
        本来就要靠省略号 + 悬停兜底（预算已在 _populate_models 里算好）。
        实测：1200 窗口 → 200 / 340 / 598；1100 最小窗口 → 200 / 340 / 498（不触发让位）。

        返回 (chip_w, model_w, right_w)，供 _populate_* 算省略号可用宽度。
        """
        def _col0(card_w):
            return max(S(80), card_w - S(PICKER_CARD_PAD) - S(TREE_SCROLL_W))

        # 左栏宽度跟随实测需求（平台名 / 芯片组名最长者），夹在 [MIN, DEFAULT] 之间
        chip = max(S(PICKER_CHIP_W_MIN),
                   min(S(PICKER_CHIP_W),
                       self._chip_col_need() + S(PICKER_CARD_PAD) + S(TREE_SCROLL_W)))
        model = S(PICKER_MODEL_W)
        right = S(0)
        try:
            avail = self._body_width() - self._body_pane_pad() - S(PANE_GAP) * 2
            right = avail - chip - model
            if right < S(PICKER_RIGHT_MIN):
                model = max(S(PICKER_MODEL_W_MIN),
                            avail - S(PICKER_RIGHT_MIN) - chip)
                right = avail - chip - model
            if right < S(PICKER_RIGHT_MIN):
                chip = max(S(PICKER_CHIP_W_MIN),
                           avail - S(PICKER_RIGHT_MIN) - model)
                right = avail - chip - model
            right = max(S(320), right)
            # 注意：Tk 部分选项返回值是 Tcl_Obj（如 ttk.Frame 的 width），
            # int() 会抛 TypeError —— 统一用 str() 比较，不做数值转换假设
            for w, card in ((chip, self._cf), (model, self._mf)):
                if str(card.cget("width")) != str(w):
                    card.configure(width=w)
            for tree, w in ((self.cat_tree, _col0(chip)),
                            (self.mod_tree, _col0(model))):
                if str(tree.column("#0", "width")) != str(w):
                    tree.column("#0", width=w)
                # minwidth 同步跟上（Tk"不低于 minwidth"的压回逻辑以此为准）
                if str(tree.column("#0", "minwidth")) != str(w):
                    tree.column("#0", minwidth=w)
        except Exception as _e:
            _log("三栏宽度分配失败: %r" % (_e,))
        old_c, old_m = self._cat_col, self._mod_col
        self._cat_col, self._mod_col = _col0(chip), _col0(model)
        self._pane_last = (chip, model, right)
        # 栏宽真的变了（窗口被拖着改大小）→ 就地重算两棵树的省略号，不重建
        if (old_c and old_c != self._cat_col) or (old_m and old_m != self._mod_col):
            self._schedule_retext()
        return self._pane_last

    def _schedule_retext(self):
        """防抖：连续 <Configure> 只在停手后重算一次省略号。"""
        if self._retext_job:
            try:
                self.root.after_cancel(self._retext_job)
            except Exception:
                pass
        self._retext_job = self.root.after(140, self._do_retext)

    def _do_retext(self):
        self._retext_job = None
        try:
            ind = S(CAT_INDENT)
            lead = CAT_TREE_LEAD + CAT_IND_SLOT
            col = self._cat_col
            for iid, meta in list((self._iid_full or {}).items()):
                full, depth, kids = meta
                txt = self._ellipsize(full, col - ind * depth - lead)
                if self.cat_tree.item(iid, "text") != txt:
                    self.cat_tree.item(iid, text=txt)
            # v2.2.0：中栏型号列表是**平铺**的（depth 恒为 0），可用宽 = 列宽 − 前缀开销
            mcol = self._mod_col
            for iid, full in list((self._mod_iid_full or {}).items()):
                txt = self._ellipsize(full, mcol - lead)
                if self.mod_tree.item(iid, "text") != txt:
                    self.mod_tree.item(iid, text=txt)
        except Exception as _e:
            _log("目录省略号重算失败: %r" % (_e,))

    def _ellipsize(self, text, avail, fm=None):
        """把 text 裁到不超过 avail 像素，超出部分以「…」收尾（不需要裁则原样返回）。

        与 Tk 自身的列内硬裁不同：硬裁会把最后一个字切掉一半，看起来像渲染故障；
        这里保证「能放几个字就放几个字 + 一个省略号」，并配悬停完整名称提示。
        v2.0.2：可用 fm 指定测量字体（目录树 / 右栏标题各用各的），默认目录树字体。
        """
        text = text or ""
        if avail <= 0:
            return ""
        fm = fm if fm is not None else self._fm_tree
        try:
            if self._px(text, fm) <= avail:
                return text
            we = self._px("…", fm)
            lo, hi = 0, len(text)
            while lo < hi:                      # 二分找「最长可用前缀」
                mid = (lo + hi + 1) // 2
                if self._px(text[:mid], fm) + we <= avail:
                    lo = mid
                else:
                    hi = mid - 1
            if lo <= 0:
                return "…"
            return text[:lo].rstrip() + "…"
        except Exception:
            return text

    # ------------------------------------------------- 目录树超长名悬停提示
    def _bind_tree_tip(self):
        """目录树悬停：型号名被省略号截断时，浮窗显示**完整名称**。"""
        self.cat_tree.bind("<Motion>", self._on_tree_motion, add="+")
        self.cat_tree.bind("<Leave>", lambda _e: self._hide_tip(), add="+")
        self.cat_tree.bind("<Button-1>", lambda _e: self._hide_tip(), add="+")
        self.cat_tree.bind("<MouseWheel>", lambda _e: self._hide_tip(), add="+")

    def _on_tree_motion(self, ev):
        try:
            iid = self.cat_tree.identify_row(ev.y)
            meta = self._iid_full.get(iid) if iid else None
            full = meta[0] if meta else None
            shown = self.cat_tree.item(iid, "text") if iid else ""
            if full and full != shown:          # 只在被截断时才弹提示
                self._show_tip(full, ev.x_root, ev.y_root)
            else:
                self._hide_tip()
        except Exception:
            self._hide_tip()

    # ------------------------------ 中栏型号列表悬停提示（v2.2.0）
    def _bind_mod_tip(self):
        """中栏型号列表悬停：型号名被省略号截断时，浮窗显示**完整名称**。

        与左栏同源的能力 —— 三栏后中栏只有 ~277px 可用，超长型号名必然走省略号，
        悬停看全名就成了必需（用户已确认接受这个取舍）。
        """
        self.mod_tree.bind("<Motion>", self._on_mod_motion, add="+")
        self.mod_tree.bind("<Leave>", lambda _e: self._hide_tip(), add="+")
        self.mod_tree.bind("<Button-1>", lambda _e: self._hide_tip(), add="+")
        self.mod_tree.bind("<MouseWheel>", lambda _e: self._hide_tip(), add="+")

    def _on_mod_motion(self, ev):
        try:
            iid = self.mod_tree.identify_row(ev.y)
            full = self._mod_iid_full.get(iid) if iid else None
            shown = self.mod_tree.item(iid, "text") if iid else ""
            if full and full != shown:          # 只在被截断时才弹提示
                self._show_tip(full, ev.x_root, ev.y_root)
            else:
                self._hide_tip()
        except Exception:
            self._hide_tip()

    def _show_tip(self, text, x, y, wrap=None):
        """在鼠标右下方弹一个只读浮窗（完整文本 / 完整路由）。

        v2.0.3 两处改进：
          · wrap —— 长文本（BIOS 更新说明 100+ 字、长文件路径）必须换行，
            否则一行 1000+px 会把提示框整条推出屏幕，等于没提示。
          · 边界收拢 —— 旧版写死 `鼠标 + (14, 22)`，贴屏幕右下角时浮窗右缘
            实测 3038 > 屏宽 2560、底缘 1472 > 屏高 1440，内容直接看不见。
            现在改为「先量尺寸 → 右边放不下翻到左侧、下边放不下翻到上方 →
            再夹进虚拟屏幕范围」（虚拟屏幕覆盖多显示器，含左/上副屏的负坐标）。
        """
        if self._tip is not None and self._tip_txt == text and self._tip_wrap == wrap:
            return                              # 同一内容已显示，不重建（免闪烁）
        self._hide_tip()
        try:
            tw = tk.Toplevel(self.root)
            tw.wm_overrideredirect(True)
            tw.configure(bg=C_BORDER)
            lb = tk.Label(tw, text=text, font=FONT_S, bg=C_CARD, fg=C_TEXT,
                          padx=S(8), pady=S(4), justify="left")
            if wrap:
                lb.configure(wraplength=wrap)
            lb.pack(padx=S(1), pady=S(1))
            tw.update_idletasks()               # 量出真实尺寸（含内边距 / 边框）
            w, h = tw.winfo_reqwidth(), tw.winfo_reqheight()
            ox, oy = S(14), S(22)
            px, py = x + ox, y + oy
            try:
                vx, vy = self.root.winfo_vrootx(), self.root.winfo_vrooty()
                vw, vh = self.root.winfo_vrootwidth(), self.root.winfo_vrootheight()
            except Exception:
                vx = vy = 0
                vw, vh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            if vw <= 1 or vh <= 1:
                vx = vy = 0
                vw, vh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            if px + w > vx + vw - S(4):         # 右边放不下 → 翻到鼠标左侧
                px = x - w - ox
            if py + h > vy + vh - S(4):         # 下边放不下 → 翻到鼠标上方
                py = y - h - S(6)
            px = max(vx + S(2), min(px, vx + vw - w - S(2)))
            py = max(vy + S(2), min(py, vy + vh - h - S(2)))
            # ⚠ 顺序不能反：必须先 -topmost 再 wm_geometry。
            # 实测（Windows / Tk 8.6.12）wm_geometry 之后调 attributes("-topmost")
            # 会把窗口位置打回 (0,0) —— 旧版正是这个顺序，所以提示浮窗其实一直
            # 弹在屏幕左上角而不是鼠标旁边，只是没人注意。
            #   顺序 A（旧）：geo→top  ⇒ winfo_x/y = (0,0)
            #   顺序 B（本版）：top→geo  ⇒ winfo_x/y = (334,282) ✔
            try:
                tw.attributes("-topmost", True)
            except Exception:
                pass
            tw.wm_geometry("+%d+%d" % (px, py))
            self._tip, self._tip_txt, self._tip_wrap = tw, text, wrap
        except Exception:
            self._tip, self._tip_txt, self._tip_wrap = None, None, None

    def _hide_tip(self):
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip, self._tip_txt, self._tip_wrap = None, None, None

    # ------------------------------------------------- 右栏型号标题自适应（v2.0.2）
    def _set_board_title(self, title):
        """设定右栏型号标题（外部唯一入口）。

        v2.0.2：标题行是「左侧标题 + 右侧『打开官网支持页』按钮」的 pack 布局，
        标题按自己的请求宽度先占位，超长型号名（华硕目录里最长 62 字符）会把
        右边按钮挤成 0 宽 —— 按钮直接看不见。这里改为先存完整名，再按当前行宽
        截断显示（省略号），完整名悬停可见。
        """
        self._title_full = title or ""
        self._fit_board_title()
        try:
            self.root.after_idle(self._fit_board_title)   # 布局落定后再校一次
        except Exception:
            pass

    def _fit_board_title(self):
        """按标题行当前可用宽度重算标题显示文本（不改变 _title_full）。

        v2.0.2：**用控件实测宽复核，而不是只信字体测量**。ttk.Label 的请求宽度
        除文本本身外还含主题内边距，单靠 font measure 估算会差十几像素、把按钮
        挤出可视区。这里做「截断 → 实测 → 还超就按超出量再收紧」的闭环，
        收敛后必然满足「标题 + 空隙 + 按钮 ≤ 行宽」。
        """
        try:
            full = self._title_full or ""
            trow = self._trow_board
            lbl = self._lbl_board_title
            btn = self.btn_openpage
            w = trow.winfo_width()
            if w <= 1:
                # 尚未布局（窗口没映射）→ 先原样显示，等 <Configure> 再来一次
                if self.v_board_title.get() != full:
                    self.v_board_title.set(full)
                return
            gap = S(BOARD_TITLE_GAP)
            avail = w - btn.winfo_reqwidth() - gap
            for _ in range(5):
                if avail <= S(24):
                    break
                shown = self._ellipsize(full, avail, self._fm_board)
                if self.v_board_title.get() != shown:
                    self.v_board_title.set(shown)
                over = lbl.winfo_reqwidth() + btn.winfo_reqwidth() + gap - w
                if over <= 0:
                    return          # 实测放得下：收工
                # v2.2.0 修：旧版这里还挂着 `or shown == full` 的提前收工条件 ——
                # 只要字体测量认为完整名放得下就返回，**忽略了 ttk.Label 自带的内边距**。
                # 旧版右栏 620px 有余量，误差一直没显形；三栏后右栏收到 498px，
                # 「ROG STRIX B850-A GAMING WIFI」这种中等长度名就差 5px 顶出去、
                # 把右侧按钮压窄（A6 实测 需=463 行宽=458）。
                # 去掉该条件后仍然收敛：_ellipsize 按字体测量截断，实测超出的部分
                # 下一轮继续收，两轮内到达 over ≤ 0。
                avail -= over + S(2)   # 还超 -> 按实测超出量再收一刀
            self.v_board_title.set(self._ellipsize(full, avail, self._fm_board))
        except Exception as _e:
            _log("右栏标题自适应失败: %r" % (_e,))

    def _tip_board_title(self, ev):
        """标题被截断时悬停看完整型号名。"""
        full = self._title_full or ""
        if full and full != self.v_board_title.get():
            self._show_tip(full, ev.x_root, ev.y_root)

    # ------------------------------------------------- 目录树折叠记忆（v2.0.3）
    def _capture_open(self):
        """把当前树的展开态记入记忆集（重建前调用）。

        不依赖 <<TreeviewOpen>> / <<TreeviewClose>> 事件：Tk 的这两个事件**不带
        iid**，只能靠 focus() 反推，而「点箭头 / 键盘左右键 / 程序自己 item(open=)」
        三条路径的 focus 行为并不一致（程序设置根本不发事件）。直接扫一遍当前树
        既准确又便宜 —— 实测 <2ms。

        v2.2.0：左栏收成**两级**（平台 → 芯片组），芯片组是叶子、没有"展开"可言，
        所以只记「被折叠的平台」。这反而让语义更干净：记忆里没记录 = 用默认值
        （展开）。
        """
        if not self._iid_full:
            return
        cl = set()
        try:
            for pl in self.cat_tree.get_children():
                mp = self._iid_full.get(pl)
                if mp and not self.cat_tree.item(pl, "open"):
                    cl.add(mp[0])
        except Exception as _e:
            _log("折叠记忆快照失败: %r" % (_e,))
            return
        self._closed_pl = cl

    def _sync_cat_all_btn(self):
        """展开/折叠按钮的文案跟随当前记忆态。"""
        try:
            t = "展开全部" if self._closed_pl else "折叠全部"
            if str(self.btn_cat_all.cget("text")) != t:
                self.btn_cat_all.configure(text=t)
        except Exception:
            pass

    def _toggle_all_cs(self):
        """一键展开 / 折叠全部平台。

        v2.0.3 的原始意图是「60+ 个芯片组、900+ 款型号，想纵览一遍要点几十次箭头」。
        v2.2.0 收成两级后，芯片组本身就是叶子 —— 按钮作用于**平台层**：
        折叠全部 = 左栏收成 3 行（先选平台再看芯片组），展开全部 = 三个平台的
        芯片组一次铺开。仍是同步动作（实测重绘 <10ms，不需要确认框）。
        """
        try:
            pls = list(self.cat_tree.get_children())
        except Exception:
            pls = []
        if self._closed_pl:
            self._closed_pl = set()
        else:
            self._closed_pl = {
                (self._iid_full.get(p) or ("",))[0] for p in pls}
        self._skip_capture = True     # 记忆是本次刚设定的，别让快照覆盖回去
        self._populate_tree()
        self._hide_tip()

    def _populate_tree(self):
        """按当前设置（平台 / 芯片组过滤）+ 搜索词重建**左栏芯片组树**（两级）。

        v2.2.0：左栏从三级（平台→芯片组→型号）收成**两级**，型号层搬到中栏
        （_populate_models）。层级的"展开"由此变成列导航。
        v2.4.0 的搜索语义随之下移：命中型号时左栏只保留「含命中的芯片组」，
        命中结果本身在中栏**跨组平铺**（用户已确认此取舍），命中数与
        「去官网搜索 ↗」出口都保留。
        v2.0.3：重建前拍展开态快照、记下滚动位置，重建后原样恢复 ——
        改设置 / 点 ↻ 刷新 / 搜索往返都不再把用户的折叠状态与浏览位置清掉。
        """
        show_plat = self.cfg.get("show_platforms") or None
        chip_flt = self.cfg.get("chip_filter") or None
        q = self._search_q
        self._hide_tip()                    # v2.0.1：重建树前先收起悬停提示
        # 拍展开态快照的判据（v2.2.0 修正）：
        #   · **只看「上一次构建是不是搜索态」**（_built_q）—— 快照读的是树上
        #     「此刻」的状态，而此刻的树反映的正是用户的真实意图。
        #   · 旧版还额外要求「本次也不是搜索态」，结果是：用户折叠了平台后
        #     **紧接着**输入搜索词，这一次重建会因为 `not q` 为假而跳过快照，
        #     折叠状态就此丢失（清空搜索后平台被重新展开）。
        #   · 搜索态构建出来的树是"全部展开"的假象，照拍会把「搜索强制展开」
        #     当成用户意图，所以只要上一次是搜索态就跳过。
        #   · 程序自己刚设过记忆（_toggle_all_cs 先改记忆再重建）也不拍，
        #     否则会把自己的改动覆盖回去。
        if not self._built_q and not self._skip_capture:
            self._capture_open()
        self._skip_capture = False
        try:
            r0 = float(self.cat_tree.yview()[0]) or 0.0
        except Exception:
            r0 = 0.0
        self.cat_tree.delete(*self.cat_tree.get_children())
        self._chip_iid = {}
        self._iid_full = {}
        self._chips_visible = []
        # v2.3.5 防护：目录尚未加载（None / 缺 brands）时清空树并提示，避免 .get 崩溃
        if not self.catalog or not self.catalog.get("brands"):
            self.v_cat.set("目录加载中…")
            self._populate_models(None)
            return
        # v2.0.1：**先**定栏宽与列宽（只依赖全量目录 + 窗口宽度，不依赖树内容），
        # 再按列宽算每行的省略号预算 —— 这样超长名不会把列宽带跑。
        self._layout_panes()
        ind = S(CAT_INDENT)
        lead = CAT_TREE_LEAD + CAT_IND_SLOT
        col = self._cat_col

        def _avail(depth):
            """该层级文本可用像素。

            文字真实起点 = CAT_TREE_LEAD + depth×indent + CAT_IND_SLOT。
            v2.0.4 修正：旧公式只减 indent×depth（叶子还少减 5px），**漏掉了 Tk 给
            每个 item 预留的 15px 展开箭头槽** —— 那一层因此多算 12px，超长名会被
            Tk 硬裁（末尾连省略号都没有）。
            """
            return col - ind * depth - lead

        def _put(iid, full, depth, kids):
            """写入节点文本（必要时省略号）+ 记下完整名供悬停提示。"""
            self.cat_tree.item(iid, text=self._ellipsize(full, _avail(depth)))
            self._iid_full[iid] = (full, depth, kids)

        n_vis = 0
        for b in self.catalog.get("brands") or []:
            name = b["name"]
            if show_plat and name not in show_plat:
                continue
            chips = [cs for cs in b.get("chipsets") or []
                     if not (chip_flt and cs["name"] not in chip_flt)]
            if q:
                chips = [cs for cs in chips
                         if q in (cs.get("name") or "").lower()
                         or any(q in (bd.get("title") or "").lower()
                                for bd in (cs.get("boards") or []))]
            n_brand = sum(len(cs.get("boards") or []) for cs in chips)
            if n_brand == 0:
                continue
            # 平台默认展开；用户手动折叠过的（_closed_pl）保持折叠，**搜索时一律展开**
            #（否则命中项藏在折叠的平台里，用户搜了却什么都看不到）。
            # 注意记忆的键必须与 _capture_open 写入的一致 —— 都用**显示标签**
            # （"Intel 主板（554 款）"），不能用裸品牌名（"Intel"），否则永远匹配不上。
            p_label = "%s 主板（%d 款）" % (name, n_brand)
            p_iid = self.cat_tree.insert(
                "", "end", open=(bool(q) or p_label not in self._closed_pl),
                text="", tags=("platform",))
            _put(p_iid, p_label, 0, True)
            for cs in chips:
                boards = cs.get("boards") or []
                if not boards:
                    continue
                cs_label = "%s（%d 款）" % (cs["name"], len(boards))
                c_iid = self.cat_tree.insert(p_iid, "end", open=False,
                                             text="", tags=("chipset",))
                _put(c_iid, cs_label, 1, True)
                self._chip_iid[c_iid] = cs
                self._chips_visible.append(cs)
            n_vis += n_brand
        total_all = self.catalog.get("total", 0)
        if q:
            pass                      # 命中数由 _populate_models 汇总后写入 v_cat
        elif n_vis == 0:
            self.v_cat.set("0 款")
            self._set_status("当前设置下没有可显示的型号，请点右上角「设置」调整范围", "warn")
        elif n_vis == total_all:
            self.v_cat.set("共 %d 款" % n_vis)
        else:
            self.v_cat.set("%d / %d 款" % (n_vis, total_all))
        if self.cat_updated:
            self.v_cat_upd.set("目录更新: %s" % self.cat_updated)
        # ---------- 中栏：搜索态 → 跨组平铺命中；非搜索态 → 当前芯片组的型号
        if q:
            self._populate_models(None)
        else:
            # 优先沿用当前芯片组（它可能已被设置过滤掉 → 退到第一个）
            want = (self.cur_chipset or {}).get("name")
            target = None
            for iid, cs in self._chip_iid.items():
                if cs.get("name") == want:
                    target = (iid, cs)
                    break
            if target is None and self._chip_iid:
                target = next(iter(self._chip_iid.items()))
            if target:
                # 程序自己设选中。无需布尔抑制标志：紧随其后的 _populate_models
                # 会把 cur_chipset 设成 target[1]，而 <<TreeviewSelect>> 队列投递
                # 到达时 _on_tree_select 比对身份即可判定「就是当前这一个」→ 跳过。
                self.cat_tree.selection_set(target[0])
                # ⚠ 只有目标**当前可见**时才 see()：Tk 的 Treeview.see() 会
                # **强行展开祖先节点**，而它的文档语义只是"滚动到可见"。
                # 实测后果：用户折叠了某个平台后，任何一次重建（改设置 / ↻ 刷新 /
                # 搜索往返）都会因为这条 see() 把平台重新顶开，折叠记忆形同虚设
                #（A15.1 / A16.3 就是这么抓出来的）。
                _p = self.cat_tree.parent(target[0])
                if _p and self.cat_tree.item(_p, "open"):
                    self.cat_tree.see(target[0])
                self._populate_models(target[1])
            else:
                self._populate_models(None)
        # v2.0.3：恢复重建前的滚动位置（**必须无条件执行**）。
        # 两个理由：① 放在 see() 之后 —— see 会主动滚动，先 moveto 会被它盖掉；
        # ② 不能像旧版那样用 `if r0 > 0.001` 过滤 —— 首次构建时 r0 恰为 0.0，
        # 而 see() 在控件尚未布局、视口高度未知时会把树**多滚一行**，
        # 结果第一个平台行被顶出视口（实测 yview 从 0.0 变成 0.0149 = 1/67 行）。
        try:
            self.cat_tree.yview_moveto(r0)
        except Exception:
            pass
        self._built_q = q      # 供下一次重建判断「上次是不是搜索态」
        self._sync_cat_all_btn()

    def _populate_models(self, chipset):
        """重建**中栏型号列表**（平铺）。

        chipset = chipset dict → 该芯片组下的型号
        chipset = None 且处于搜索态 → 跨芯片组的全部命中型号（同一芯片组名命中时
                                     整组收入，否则只收标题命中的型号）
        chipset = None 且非搜索态 → 空列表（点击左栏芯片组之前的状态）

        v2.2.0 新增。与 _populate_tree 共用同一套省略号预算口径：
        中栏是平铺列表（depth 恒为 0），可用宽 = 列宽 − 前缀开销。
        """
        self.cur_chipset = chipset
        lead = CAT_TREE_LEAD + CAT_IND_SLOT
        q = self._search_q
        self._mod_iid_full = {}
        self.iid_board = {}
        try:
            self.mod_tree.delete(*self.mod_tree.get_children())
        except Exception:
            pass
        if chipset is not None:
            boards = list(chipset.get("boards") or [])
            if q:
                # 搜索态下仍聚焦当前芯片组：芯片组名命中 → 整组；否则只留标题命中
                if q not in (chipset.get("name") or "").lower():
                    boards = [bd for bd in boards
                              if q in (bd.get("title") or "").lower()]
            self.v_scope.set(chipset.get("name") or "")
        elif q:
            boards = []
            for cs in self._chips_visible:
                bs = cs.get("boards") or []
                if q in (cs.get("name") or "").lower():
                    boards.extend(bs)          # 芯片组名命中 → 整组收入
                else:
                    boards.extend([bd for bd in bs
                                   if q in (bd.get("title") or "").lower()])
            self.v_scope.set("搜索结果")
        else:
            boards = []
            self.v_scope.set("")
        avail = self._mod_col - lead
        for bd in boards:
            iid = self.mod_tree.insert("", "end", text="", tags=("board",))
            full = bd.get("title") or ""
            self.mod_tree.item(iid, text=self._ellipsize(full, avail))
            self._mod_iid_full[iid] = full
            self.iid_board[iid] = bd
        if q:
            self._search_hits = len(self.iid_board)
            self.v_cat.set("命中 %d 款" % len(self.iid_board))
        # 搜索无命中 → 显示「去官网搜索」出口
        try:
            if q and not self.iid_board:
                if not self._nores.winfo_manager():
                    self._nores.grid()
            elif self._nores.winfo_manager():
                self._nores.grid_remove()
        except Exception:
            pass
        # 保持当前选中型号仍在列表中可见。
        # ⚠ 不做布尔抑制：Tk 的 <<TreeviewSelect>> 是队列投递的，标志在事件送达时
        # 早已复位 → 旧写法会让程序设选中触发一次重复的 BIOS 查询。改为在
        # _on_mod_select 里比对 slug（「选中的是不是已加载的那个型号」），时机无关。
        if self.cur_slug:
            for iid, bd in self.iid_board.items():
                if (bd.get("slug") or "") == self.cur_slug:
                    self.mod_tree.selection_set(iid)
                    self.mod_tree.see(iid)
                    break

    def _on_tree_select(self, _ev=None):
        """左栏选中芯片组 → 重建中栏型号列表（**不触发** BIOS 查询）。

        ⚠ 守卫必须与事件投递时机无关。Tk 的 `<<TreeviewSelect>>` 是**队列投递**的，
        程序自己 `selection_set()` 产生的那一次会在下一个事件循环才送达 ——
        此时 any 布尔抑制标志早被复位。因此这里比对「选中的芯片组对象是否就是
        当前展示的那个」（同一份目录数据，dict 身份稳定），既等效又时机无关。
        """
        sel = self.cat_tree.selection()
        if not sel:
            return
        cs = self._chip_iid.get(sel[0])
        if cs is not None and cs is not self.cur_chipset:
            self._populate_models(cs)

    def _on_mod_select(self, _ev=None):
        """中栏选中型号 → 查询该型号的 BIOS（原左栏叶子节点的行为，v2.2.0 平移）。

        同上：程序同步选中（_populate_models 里恢复"当前选中型号"）也会走到这里。
        用 slug 比对而不是布尔标志 —— 判据是「选中的是不是已经加载的那个型号」，
        与事件何时送达无关；顺带消掉了旧版「树一重建就重查同一型号」的浪费
        （搜索输入、展开/折叠、改设置都会重建中栏）。
        """
        sel = self.mod_tree.selection()
        if not sel:
            return
        hit = self.iid_board.get(sel[0])
        if hit is None:
            return
        if (hit.get("slug") or "") == self.cur_slug:
            return
        self._load_board(hit)

    # ------------------------------------------------------------- BIOS 查询
    def _load_board(self, board):
        slug = (board or {}).get("slug") or ""
        title = (board or {}).get("title") or ""
        if not slug:
            messagebox.showwarning(APP_TITLE, "该型号缺少有效的官网标识（%s）" % title, parent=self.root)
            return
        if self._loading_slug == slug:     # 同一型号查询已在途，跳过重复请求
            return
        self._loading_slug = slug
        self.cur_board = board
        self.cur_slug = slug
        self.cur_title = title
        # prod 型号先查全球站（特供回退 cn）；ROG 型号直接走 ROG 源
        self.cur_src = (board or {}).get("src") or "prod"
        self.cur_host = "rog" if self.cur_src == "rog" else "com"
        self._set_board_title(title)
        self.v_board_sub.set(slug)
        self.lbl_host.configure(text="")
        self.v_stat_right.set("数据源：查询中…   ·   %s" % _ver_suffix())
        # v2.4.0：立刻切到「查询中」加载态（旧版右侧一片空白，只有状态栏一行字）
        self.groups = {}
        self.badges = []
        self.cur_items = []
        self._latest_iids = set()
        self._iid_item = {}
        self._hover_row = None
        self.tv.delete(*self.tv.get_children())
        if self.grow.winfo_manager():
            self.grow.pack_forget()
        if self._panel_note_box.winfo_manager():
            self._panel_note_box.pack_forget()
        self._start_loading(title)
        self._set_status("正在查询 %s 的 BIOS 信息…" % title, "run")
        self.q.put(("busy", True))
        self._spawn(self._board_worker, board)

    def _board_worker(self, board):
        """后台查询 BIOS 面板（华硕三路径：prod 全球站 / prod 中国站回退 / ROG 专用接口）。

        v2.0.0 修复：无论成功 / 失败都必须发 busy=False 解除主按钮禁用 ——
        旧版成功分支漏发，导致选完型号后「下载选中版本」永久停在 disabled 暗红。
        """
        slug = (board or {}).get("slug") or ""
        title = (board or {}).get("title") or ""
        err_host = "rog" if (board or {}).get("src") == "rog" else "com"
        try:
            panel, host = self.api.get_bios_panel(board, "bios")
            if panel is None:
                self.q.put(("board", (slug, title, None, None,
                                      "官网无 BIOS 数据（可能尚未发布或型号有误）", host)))
                return
            groups, _os, note, _ptitle = self.api.parse_bios_groups(panel)
            if not groups:
                self.q.put(("board", (slug, title, None, None, "该型号 BIOS 列表为空", host)))
                return
            self.q.put(("board", (slug, title, groups, note, None, host)))
        except Exception as e:
            self.q.put(("board", (slug, title, None, None,
                                  "查询失败：%s" % str(e)[:300], err_host)))
        finally:
            self.q.put(("busy", False))

    def _apply_board(self, data):
        slug, title, groups, note, err, host = data
        if self._loading_slug == slug:      # 该型号查询已结束（成功/失败均清锁）
            self._loading_slug = None
        if slug != self.cur_slug:      # 过期结果，丢弃
            return
        self.cur_host = host if host in ("com", "cn", "rog") else "com"
        # v2.4.0：数据源徽章（副标题行 + 状态栏右侧），明确当前读的是哪个站
        host_txt = {"com": "华硕全球站", "cn": "华硕中国站",
                    "rog": "ROG 玩家国度站"}.get(self.cur_host, self.cur_host)
        try:
            self.lbl_host.configure(text="· 数据源 %s" % host_txt)
            self.v_stat_right.set("数据源：%s   ·   %s" % (host_txt, _ver_suffix()))
        except Exception:
            pass
        if groups is None:
            self._set_board_title(title)
            self.groups = {}
            self.badges = []
            self.cur_items = []
            self._latest_iids = set()
            self._iid_item = {}
            self.tv.delete(*self.tv.get_children())
            if self.grow.winfo_manager():
                self.grow.pack_forget()
            # v2.3.5：显示占位并保留型号标题/slug（仍可点「打开官网支持页」核实）
            self._show_empty(True, title=title, sub=err or "暂无可用 BIOS 数据", keep_title=True)
            self._set_status(err or "查询失败", "err")
            return
        self.groups = groups
        self.badges = list(groups.keys())
        # 多分组才显示类型下拉
        if len(self.badges) > 1:
            self.grp_box["values"] = ["%s（%d 项）" % (b, len(groups[b])) for b in self.badges]
            self.grp_box.current(0)
            if not self.grow.winfo_manager():
                # v2.4.0 grid 布局：grow 放 _list_zone 顶部（rf row=2 内的 pack）
                self.grow.pack(fill="x", pady=(S(0), S(6)))
        else:
            if self.grow.winfo_manager():
                self.grow.pack_forget()
        self._load_badge(self.badges[0])
        if note:
            note_txt = re.sub(r"<[^>]+>", " ", str(note)).strip()
            # 主板级提示放主板标题下方的固定提示框（多语言/URL 自然 wrap）
            self.v_panel_note.set(note_txt)
            if not self._panel_note_box.winfo_manager():
                # v2.4.0 grid 布局：_panel_note_box 在 _list_zone 内 pack 顶部
                self._panel_note_box.pack(fill="x", pady=(S(0), S(6)))
        else:
            self.v_panel_note.set("")
            if self._panel_note_box.winfo_manager():
                self._panel_note_box.pack_forget()
        self._set_status("%s · 共 %d 个 BIOS 版本%s" %
                         (title, len(self.cur_items),
                          "" if self.cur_host == "com" else
                          "（数据源：%s）" % SRC_LABEL.get(self.cur_host, self.cur_host)), "ok")

    def _on_group_changed(self, _ev=None):
        i = self.grp_box.current()
        if 0 <= i < len(self.badges):
            self._load_badge(self.badges[i])

    # ------------------------------------------------- 更新说明列按像素截断（v2.0.2）
    def _desc_px(self):
        """「更新说明」列当前可用像素宽（stretch 列，随窗口变化）。

        v2.0.5：预留量由 S(14) 提到 S(20)。Tk 的树表单元格左右各有内边距，
        14px 只够一边 —— 实测单元格 214px 时文字从「单元格 x + 2」起排，
        右侧只剩 214 − 2 − 198 = 14px，末尾的「…」紧贴列右缘（8 倍放大像素复核时
        几乎分辨不出）。留 20px 后余量稳定，可见字数只少不到 1 个字。
        """
        try:
            w = int(self.tv.column("note", "width"))
        except Exception:
            w = S(380)
        return max(S(40), w - S(20))

    def _fit_desc(self, text):
        """把更新说明裁到列宽内，超出以省略号收尾。

        v2.0.2：旧版是 `desc[:56]` —— **按字符数**截断。字符数只是像素的粗糙代理：
        实测 DPI 150% 下说明列 880px，56 字符只占 589px，白白浪费三分之一的列宽；
        而且是硬切到句子中间、没有省略号，用户看不出后面还有内容。改为按列**实测
        像素**截断（复用目录树那套 _ellipsize）。
        """
        return self._ellipsize(text or "", self._desc_px(), self._fm_tree)

    def _layout_tv_columns(self):
        """按控件实测宽度重排 BIOS 列表列宽 —— 让「更新说明」列吃掉剩余宽度。

        v2.0.5：Tk 的 `stretch` **只在有富余时分配**，列宽总和超过控件宽度时
        不会收缩 → 最后一列被**硬裁**（末尾连省略号都没有，表头「更新说明」也被切）。
        这个隐患一直潜伏：固定三列 168+110+92 = 370，加说明列 380 = 750，
        而右栏要 ≥788 才不裁 —— v2.0.4 的固定 300px 上限恰好让右栏总是 788，
        所以从没显形；v2.0.5 左栏按需放宽后右栏收到 620px 保底，立刻暴露。
        改为在 <Configure> 里先按实测宽度重排，再算省略号预算（_desc_px 读的就是它）。
        """
        if self._laying_cols:
            return None
        self._laying_cols = True
        try:
            avail = int(self.tv.winfo_width())
            if avail < S(120):          # 首次布局前拿不到真实宽度
                return None
            fixed = 0
            for cid, base in TV_COL_BASE:
                w = S(base)
                fixed += w
                if str(self.tv.column(cid, "width")) != str(w):
                    self.tv.column(cid, width=w)
            note = max(S(TV_NOTE_MIN), avail - S(TV_COL_OVER) - fixed)
            if str(self.tv.column("note", "width")) != str(note):
                self.tv.column("note", width=note)
            return note
        except Exception as _e:
            _log("列表列宽自适应失败: %r" % (_e,))
            return None
        finally:
            self._laying_cols = False

    def _on_tv_configure(self, _ev=None):
        """列表尺寸变化 → 先按新宽度重排列，再防抖重算各行省略号。"""
        self._layout_tv_columns()
        if self._tv_retext_job:
            try:
                self.root.after_cancel(self._tv_retext_job)
            except Exception:
                pass
        self._tv_retext_job = self.root.after(140, self._do_desc_retext)

    def _do_desc_retext(self):
        """只重写「更新说明」单元格，不重建列表（保住选中与滚动位置）。"""
        self._tv_retext_job = None
        try:
            for iid in list(self._iid_item.keys()):
                desc = self._row_desc(iid)
                vals = list(self.tv.item(iid, "values"))
                if len(vals) < 4:
                    continue
                new = self._fit_desc(desc)
                if vals[3] != new:
                    vals[3] = new
                    self.tv.item(iid, values=tuple(vals))
        except Exception as _e:
            _log("更新说明重算失败: %r" % (_e,))

    # ------------------------------------------- 选中版本详情面板（v2.2.0 新增）
    def _show_detail(self, item, hint=None):
        """把选中 BIOS 版本的完整信息填进右栏下半的详情面板。

        item = None → 空态（未选型号 / 查询中 / 该型号无数据 / 列表里还没选版本）。
        有 item 时展示：版本号 + 发布日期 + 大小、更新说明**全文**、SHA-256，
        并给出「下载此版本」与「复制下载链接」两个上下文动作。

        这块面板的由来：旧版把整整 240px 高空在 BIOS 列表下方（只有 4 个版本时
        占列表区 58%），而更新说明全文与 SHA-256 只能靠悬停或拉宽列才看得到。
        """
        self._det_item = item
        if getattr(self, "_det_body", None) is None:
            return          # _build_ui 早期（空态先于详情面板创建）时的安全出口
        try:
            if not item:
                self._det_body.grid_remove()
                if hint:
                    self._det_empty.configure(text=hint)
                    self._det_empty.grid()
                    self._show_detail_zone(True)
                else:
                    # v2.3.0：还没选型号时整块详情区收起。
                    # 旧版这里照旧显示「在上方列表中选择一个版本…」，与右栏上方
                    # 的大空态叠成两个空盒子，同一件事说了两遍。
                    self._det_empty.grid_remove()
                    self._show_detail_zone(False)
                return
            ver = clean_version(item.get("download_version"))
            # 与列表里的「最新」前缀保持一致（cur_items 已按发布日期倒序）
            if self.cur_items and item is self.cur_items[0] and ver:
                ver = "★ " + ver
            self.v_det_title.set("版本 %s" % (ver or "—"))
            try:
                size = "%.1f MB" % (int(item.get("download_size") or 0) / 1048576.0)
            except Exception:
                size = "—"
            date = (item.get("download_release") or "").strip() or "—"
            self.v_det_meta.set("%s   ·   %s" % (date, size))
            desc = re.sub(r"<[^>]+>", " ",
                          str(item.get("download_description") or "")).strip()
            self._det_desc_full = desc or "（官网未提供更新说明）"
            self.v_det_desc.set(self._clip_desc(self._det_desc_full))
            sha = parse_sha256_from_field(item.get("download_sha256"))
            self._det_sha_full = sha or ""
            self._det_empty.grid_remove()
            self._det_body.grid()
            self._show_detail_zone(True)
            # 先让面板上屏（宽度才有意义），再按实际宽度排 SHA
            self._refresh_det_sha()
        except Exception as _e:
            _log("详情面板刷新失败: %r" % (_e,))

    def _show_detail_zone(self, on):
        """详情区（含上方发丝分隔线）的显隐。

        v2.3.0：没有选中型号时整块收起 —— 旧版会与右栏大空态叠成两个空盒子。
        """
        try:
            if on:
                if not self._detail_sep.winfo_manager():
                    self._detail_sep.grid()
                if not self._detail_zone.winfo_manager():
                    self._detail_zone.grid()
            else:
                self._detail_zone.grid_remove()
                self._detail_sep.grid_remove()
        except Exception as _e:
            _log("详情区显隐失败: %r" % (_e,))

    def _det_sha_avail(self):
        """详情面板里 SHA-256 一行可用的像素宽（取面板宽，不取标签自身宽）。

        ⚠ 不能拿 `_det_sha_lbl.winfo_width()`：标签被父容器压缩后，它的实测宽
        恰好等于"被裁后的宽"，拿它算预算等于自己证明自己放得下 —— 空转。
        """
        try:
            w = self._detail_zone.winfo_width() - S(24)
        except Exception:
            w = 0
        if w <= S(120):
            try:
                w = self._det_desc.winfo_width() - S(6)
            except Exception:
                w = S(480)
        return max(S(80), int(w))

    def _refresh_det_sha(self):
        """按当前面板宽度重排 SHA-256：放得下给全文，放不下给「头…尾」。"""
        full = self._det_sha_full
        head = "SHA-256  "
        if not full:
            self.v_det_sha.set(head + "官网未提供")
            try:
                self._det_sha_lbl.configure(cursor="")
            except Exception:
                pass
            return
        avail = self._det_sha_avail()
        if self._ss_px(head + full) <= avail:
            self.v_det_sha.set(head + full)
            return
        # 从**最长**的头段往下收，找到第一个放得下的长度。
        # 首版写成「从 16 开始向下减」，方向反了 —— 可用 515px 时只用了 256px，
        # 白白丢掉一半宽度（A26.23 一开始没抓住，见其"利用率"判据）。
        n = max(6, len(full) - 9)
        while n > 6:
            if self._ss_px("%s%s…%s" % (head, full[:n], full[-8:])) <= avail:
                break
            n -= 1
        self.v_det_sha.set("%s%s…%s" % (head, full[:n], full[-8:]))

    def _copy_det_sha(self):
        if self._det_sha_full:
            self._clipboard(self._det_sha_full)

    def _detail_char_budget(self):
        """详情面板每行可容纳的字符数（按实测字宽估算）。

        用 'x'×32 的实测宽做样本 —— 华硕的更新说明以 ASCII 为主，
        拿半角字宽换算比拿全角准得多（两者实测差近一倍）。
        """
        try:
            w = max(S(120), int(self._det_desc.winfo_width()) - S(6))
        except Exception:
            w = S(400)
        cw = self._text_px("x" * 32) / 32.0
        if cw <= 0.5:
            cw = 8.0
        return max(16, int(w / cw))

    def _clip_desc(self, text):
        """把更新说明裁到 DETAIL_MAX_LINES 行以内（超出以「…」收尾，悬停看全文）。"""
        budget = self._detail_char_budget() * DETAIL_MAX_LINES
        if len(text) <= budget:
            return text
        return text[:max(8, budget - 1)].rstrip() + "…"

    def _on_detail_configure(self, ev=None):
        """更新说明的换行宽跟随详情面板宽度，并重裁行数上限。

        ⚠ 必须绑定在 `_detail_zone`（宽由卡片决定、与内容无关）而不是
        `_det_desc` 自己 —— 后者的宽度取决于换行结果，绑它会形成反馈环。
        """
        try:
            wid = (ev.widget if ev is not None else self._detail_zone)
            w = int(wid.winfo_width())
            if w > S(60):
                self._det_desc.configure(wraplength=max(S(120), w - S(26)))
            if self._det_desc_full:
                self.v_det_desc.set(self._clip_desc(self._det_desc_full))
            # v2.3.0：SHA-256 的省略位置也要随宽度重算（窗口拉宽后应当尽量给全文）
            if self._det_sha_full:
                self._refresh_det_sha()
        except Exception:
            pass

    def _load_badge(self, badge):
        self.cur_badge = badge
        items = list(self.groups.get(badge) or [])
        items.sort(key=lambda it: it.get("download_release") or "", reverse=True)
        self.cur_items = items
        self._hover_row = None
        self._latest_iids = set()
        self._iid_item = {}
        self.tv.delete(*self.tv.get_children())
        for i, it in enumerate(items):
            ver = clean_version(it.get("download_version"))
            # v2.4.0：最新版本在版本号前加 ★ 前缀。
            # 直接选中态会顶替 tag 的绿底（Treeview 的 selected map 永远覆盖 tag 颜色），
            # 视觉上像是"红色错误"。前缀字符即使在选中时也能让用户看出是最新行。
            # 复制菜单用 clean_version 拿原始值，菜单/匹配不受 ★ 影响。
            if i == 0:
                ver = "★ " + ver
            date = it.get("download_release") or ""
            size = it.get("download_size") or 0
            try:
                size = "%.1f MB" % (int(size) / 1048576.0)
            except Exception:
                size = ""
            desc = (it.get("download_description") or "").replace("\n", " ").strip()
            tag = ("latest", "row") if i == 0 else ("row",)
            iid = self.tv.insert("", "end", values=(ver, date, size, self._fit_desc(desc)),
                                 tags=tag)
            self._iid_item[iid] = it
            if i == 0:
                self._latest_iids.add(iid)
        if items:
            self._show_empty(False)
            self._fade_list_in()        # v2.4.0：列表淡入
            first = self.tv.get_children()[0]
            self.tv.selection_set(first)
            self.tv.focus(first)
            # v2.2.0：显式同步一次详情面板 —— <<TreeviewSelect>> 是排队投递的，
            # 若此刻选中态恰好没变化就不会触发，面板会停在旧版本上。
            self._show_detail(self._iid_item.get(first))
        else:
            self._show_empty(True)

    def _on_tv_double_click(self):
        if not self.tv.selection():
            return
        if self.dl_thread and self.dl_thread.is_alive():
            return
        self._on_download()

    # ------------------------------------------------------------- 官网页
    def _board_site_url(self):
        """拼当前型号「打开官网支持页」的目标 URL（华硕 BIOS 支持页 helpdesk_bios）。

        华硕各站（www.asus.com / www.asus.com.cn / rog.asus.com）的 BIOS 下载都在
        型号页的 helpdesk_bios 子页；目录 ProductURL 已带正确域名与型号路径，
        直接在其后追加 helpdesk_bios/ 即可。目录缺 URL 时按数据源域名回退拼接。
        """
        board = getattr(self, "cur_board", None) or {}
        raw = (board.get("url") or "").strip()
        base, path = BASE, ""
        m = re.match(r"(https?://[^/]+)(/.*)?$", raw) if raw else None
        if m:
            base = m.group(1).rstrip("/")          # 目录 URL 自带域名（含 rog.asus.com 情形）
            path = "/" + (m.group(2) or "").strip("/") + "/"
        elif raw:
            path = "/" + raw.strip("/") + "/"       # 相对路径：域名按型号来源判断
            base = CN_BASE if board.get("host") == "cn" else BASE
        if not path:
            base = CN_BASE if self.cur_host == "cn" and self.cur_src != "rog" else BASE
            slug = board.get("slug") or self.cur_slug
            if not slug:
                return base + "/motherboards-components/motherboards/all-series/"
            path = "/motherboards-components/motherboards/all-series/%s/" % slug
        return base + path + "helpdesk_bios/"

    def _on_open_site(self):
        if self.cur_slug:
            webbrowser.open(self._board_site_url())
        else:
            webbrowser.open(BASE + "/motherboards-components/motherboards/all-series/")

    # ------------------------------------------------------------- 下载
    def _remember_dir(self, d):
        """记住并**落盘** BIOS 保存目录（v2.0.2：设备可以保存）。

        旧版只存在内存里（self.last_dir），关掉程序就忘 —— 每次启动都要重新选
        一次目录。现在写进配置；写不进去（目录只读等）就退回「下次启动重选」，
        并把实情告诉用户，不装作已保存。
        """
        if not d:
            return
        self.last_dir = d
        try:
            self.v_dir.set(d)
        except Exception:
            pass
        cfg = dict(self.cfg)
        if cfg.get("save_dir") == d:
            return
        cfg["save_dir"] = d
        self.cfg = cfg
        if not save_cfg(cfg):
            self._set_status("保存目录已在本机记住，但配置写入失败：下次启动需要重新选择。",
                             "warn")

    def _on_pick_dir(self):
        d = filedialog.askdirectory(title="选择 BIOS 文件保存目录",
                                    initialdir=self.last_dir or os.path.expanduser("~"))
        if d:
            self._remember_dir(d)

    def _default_dir(self):
        if self.last_dir:
            return self.last_dir
        dl = os.path.join(os.path.expanduser("~"), "Downloads")
        return dl if os.path.isdir(dl) else os.path.expanduser("~")

    def _selected_item(self):
        """当前选中的 BIOS 条目（v2.0.0）。

        改为 iid → item 映射直查。旧版拿显示文本里的版本号回查 cur_items，
        遇到两条都缺版本号（clean_version 都返回「—」）时会取到错的那条；
        ★ 前缀也需要额外剥离，现在一并省掉。
        """
        sel = self.tv.selection()
        if not sel:
            return None
        return self._iid_item.get(sel[0])

    def _on_download(self):
        it = self._selected_item()
        if not it:
            messagebox.showwarning(APP_TITLE, "请先在列表中选择要下载的 BIOS 版本", parent=self.root)
            return
        if self.dl_thread and self.dl_thread.is_alive():
            messagebox.showwarning(APP_TITLE, "已有下载任务进行中", parent=self.root)
            return
        url = it.get("download_url")
        if not url:
            messagebox.showerror(APP_TITLE, "该版本缺少下载链接", parent=self.root)
            return
        fname = _safe_filename(url, clean_version(it.get("download_version")))
        save_dir = self._default_dir()
        self.v_dir.set(save_dir)
        if not self.last_dir:
            d = filedialog.askdirectory(title="选择 BIOS 文件保存目录", initialdir=save_dir)
            if not d:
                return
            save_dir = d
            self._remember_dir(d)      # v2.0.2：选一次就一直记得（落盘）
            self.v_dir.set(d)
        dest = os.path.join(save_dir, fname)
        self.dl_cancel.clear()
        # v2.4.0：复位进度条配色 / 测速状态 / 旧横幅
        self._toast_hide()
        self._pb_color_reset()
        self._dl_speed = None
        self._dl_last_t = None
        self._dl_last_b = 0
        # v2.0.0：先掐掉上一次的进度条补间再归零，否则旧动画会把 value 拉回去
        self.anim.cancel("pb")
        self._pb_show()          # v2.3.0：空槽在空闲时是收起的
        self._set_downloading(True)
        self.pb["value"] = 0
        self.v_pb.set("准备下载…")
        ver = clean_version(it.get("download_version"))
        expect_sha = parse_sha256_from_field(it.get("download_sha256"))
        # v2.0.2：把官网面板标注的字节数带下去做交叉校验（见 do_download）
        expect_size = int(it.get("download_size") or 0)
        self._set_status("正在下载 %s（%s）…" % (fname, ver), "run")
        self.dl_thread = self._spawn(self._dl_worker, url, dest, expect_sha, expect_size)

    def _dl_worker(self, url, dest, expect_sha, expect_size=0):
        def cb(done, total):
            self.q.put(("dl_progress", (done, total)))
            return not self.dl_cancel.is_set()
        try:
            ok, path, err, _ = do_download(url, dest, progress_cb=cb,
                                           cancel_event=self.dl_cancel,
                                           expect_size=expect_size)
            sha = None
            if ok and expect_sha:
                self.q.put(("status", "下载完成，校验 SHA-256…", "run"))
                sha = sha256_file(dest)
            self.q.put(("dl_done", (ok, dest, err, sha, expect_sha)))
        except Exception as e:
            self.q.put(("dl_done", (False, dest, repr(e)[:200], None, expect_sha)))

    def _dl_progress(self, done, total):
        # v2.4.0：速度 EMA + 剩余时间（旧版只有「已下载 / 总量」两个数字）
        now = time.monotonic()
        if self._dl_last_t is None:
            self._dl_last_t = now
            self._dl_last_b = done
        else:
            dt = now - self._dl_last_t
            if dt >= 0.35:
                inst = max(0.0, (done - self._dl_last_b) / dt)
                self._dl_speed = inst if self._dl_speed is None \
                    else 0.65 * self._dl_speed + 0.35 * inst
                self._dl_last_t = now
                self._dl_last_b = done
        if total > 0:
            pct = min(100.0, done * 100.0 / total)
            self._pb_set(pct)          # 缓动补间，不再整数跳变
            parts = ["%.1f / %.1f MB" % (done / 1048576.0, total / 1048576.0)]
            if self._dl_speed and self._dl_speed > 1024:
                parts.append(_fmt_bytes(self._dl_speed) + "/s")
                parts.append("剩 " + _fmt_eta((total - done) / self._dl_speed))
            parts.append("(%d%%)" % int(pct))
            self.v_pb.set("   ".join(parts))
        else:
            self.v_pb.set("%.1f MB" % (done / 1048576.0))

    def _dl_done(self, ok, path, err, sha, expect_sha):
        self._set_downloading(False)
        self._dl_speed = None
        self._dl_last_t = None
        if not ok:
            self._pb_color_reset()
            if err == "已取消":
                self._set_status("已取消下载", "warn")
                self._toast_show("已取消下载", ok=False, ttl=6000)
                self._pb_idle()          # v2.3.0：空槽一并收起
                return
            self._set_status("下载失败：%s" % err, "err")
            self._toast_show("下载失败：%s" % err, ok=False, ttl=12000)
            self._pb_idle()          # v2.3.0：空槽一并收起
            messagebox.showerror(APP_TITLE, "下载失败：%s" % err, parent=self.root)
            return
        base = os.path.basename(path)
        msg = "下载完成：%s" % path
        if expect_sha and sha:
            if sha == expect_sha:
                msg += "\n\nSHA-256 校验：通过 ✓"
                self._set_status("下载完成，SHA-256 校验通过", "ok")
                self._toast_show("✓ 下载完成并校验通过    %s" % base)
            else:
                msg += "\n\n警告：SHA-256 校验不一致！\n期望 %s\n实际 %s" % (expect_sha, sha)
                self._set_status("下载完成，但 SHA-256 校验不一致！", "err")
                self._toast_show("⚠ SHA-256 校验不一致，请勿直接刷写    %s" % base,
                                 ok=False, ttl=15000)
        else:
            self._set_status("下载完成", "ok")
            self._toast_show("✓ 下载完成    %s" % base)
        self._pb_set(100, dur=220)
        self.v_pb.set("")
        messagebox.showinfo(APP_TITLE, msg, parent=self.root)
        # 弹窗关闭后再播完成动效，避免动画被模态窗口挡住
        self._pb_flash(C_ACCENT, C_OK)
        self._status_flash(C_OK)
        self.root.after(1400, self._pb_color_reset)
        # v2.3.0：让 100% 与绿色脉冲先被看见，1.7s 后再收起空槽
        self.root.after(1700, self._pb_idle)

    def _on_cancel_download(self):
        """取消进行中的下载：置位 dl_cancel，下载线程轮询到后会清理 .part 并回报。"""
        if self.dl_thread and self.dl_thread.is_alive():
            self.dl_cancel.set()
            self._set_status("正在取消下载…", "warn")
            try:
                self.btn_cancel.config(state=tk.DISABLED)
            except Exception:
                pass

    def _on_open_dir(self):
        d = self.last_dir or self._default_dir()
        try:
            if os.path.isdir(d):
                os.startfile(d)  # noqa  (Windows)
            else:
                messagebox.showwarning(APP_TITLE, "目录不存在：%s" % d, parent=self.root)
        except Exception as e:
            _log("open dir err %r" % (e,))
            messagebox.showwarning(APP_TITLE, "无法打开目录：%s" % d, parent=self.root)


# ================================================================ 自检（无 GUI）
def selftest():
    """--check: 控制台自检（打包后 stdout 不可见，写日志 + 输出到文件）。

    v2.0.2：拆成「入口 + 主体」两层 ——
      · 入口负责**无条件**把日志写出去（finally），自检最需要日志的时候恰恰是
        它中途崩掉的时候，旧版把写日志放在最后一行，一崩就没日志，排障等于瞎。
      · 主体里 `cat` 先绑定 None，修掉「目录构建失败 → 后面 _first_board 引用
        未赋值的 cat 抛 NameError」这条断网必崩路径。
    """
    lines = []

    def p(m):
        lines.append(str(m))
        try:
            print(m)
        except Exception:
            pass

    rc = 0
    try:
        rc = _selftest_body(p)
    except Exception as e:
        p("\n[自检中断] 未预期的异常：%r" % (e,))
        p(traceback.format_exc())
        rc = 1
    finally:
        try:
            out = _data_path("asus_bios_checker_selftest.log")
            with open(out, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            p("（自检日志已保存: %s）" % out)
        except Exception:
            pass
    return rc


def _selftest_body(p):
    """自检主体：逐段 try，任何一段失败都不影响其余段落与最终落盘。"""
    p("=== 华硕主板 BIOS 下载工具 自检 v%s ===" % APP_VERSION)
    p("时间: " + time.strftime("%Y-%m-%d %H:%M:%S"))

    # 0 配置持久化（v2.0.3）
    # 用户报「设置改了存不住」时，第一个要回答的问题就是：配置到底写到哪个路径、
    # 那个目录真的可写吗、读回来是什么。放在最前面是因为它不依赖网络，
    # 任何环境下都能给出结论；而且**只读 + 一次探针写入**，不会破坏既有设置。
    p("\n[0] 配置持久化")
    try:
        cp = _cfg_path()
        app_d = _app_dir()
        ok_w = _dir_writable(app_d)
        p("  程序目录: %s" % app_d)
        p("  目录可写: %s（实测落一个探针文件并立即删除）" % ok_w)
        p("  配置文件: %s" % cp)
        p("  落在程序目录: %s"
          % (os.path.dirname(os.path.abspath(cp)) == os.path.abspath(app_d)))
        if os.path.isfile(cp):
            with open(cp, "r", encoding="utf-8") as f:
                p("  现有内容: %s" % f.read().strip()[:220])
        else:
            p("  现有文件: 无（首次运行，或设置从未保存过）")
        cur = load_cfg()
        p("  读取结果: %s" % json.dumps(cur, ensure_ascii=False))
        p("  结论: %s"
          % ("配置可正常读写" if ok_w else
             "⚠ 程序目录不可写，设置会退回临时目录（%s）—— 重启后可能丢失"
             % tempfile.gettempdir()))
    except Exception as e:
        p("  配置自检失败: %r" % (e,))

    api = AsusApi()
    cat = None          # v2.0.2：[2] 失败时这里为 None，下游 _first_board 才能安全跳过

    # 1 目录接口（SeriesFilter 芯片组码 + SeriesFilterResult 平台全量）
    p("\n[1] 官网目录接口（odinapi）")
    try:
        codes = api.get_chipset_codes()
        p("  芯片组码: %d 个" % len(codes))
        for c in codes[:8]:
            p("    %s %s" % (c["plat"], c["code"]))
        n_all = 0
        for cat_id, plat in ((63, "Intel"), (64, "AMD")):
            items = api.get_platform_products(cat_id)
            n_all += len(items)
            p("  %s 平台在售型号: %d 款" % (plat, len(items)))
        p("  全量在售型号: %d 款" % n_all)
    except Exception as e:
        p("  目录接口异常: %r" % (e,))

    # 2 目录构建（29 次芯片组请求）
    p("\n[2] 目录构建（Intel/AMD 归类）")
    try:
        cat = fetch_catalog(api, progress=lambda m: p("    " + m))
        save_catalog(cat)
        total = cat.get("total", 0)
        p("  总型号: %d" % total)
        for b in cat.get("brands") or []:
            p("  %s 主板: %d 款, %d 个芯片组" % (b["name"], b.get("count", 0), len(b.get("chipsets") or [])))
            for cs in (b.get("chipsets") or [])[:5]:
                p("      %s: %d 款（示例: %s）" % (cs["name"], len(cs["boards"]),
                                                    cs["boards"][0]["title"] if cs["boards"] else ""))
    except Exception as e:
        p("  目录构建异常: %r" % (e,))

    def _first_board(pred):
        if not cat:
            return None
        for b in cat.get("brands") or []:
            for cs in b.get("chipsets") or []:
                for bd in cs.get("boards") or []:
                    if pred(bd):
                        return bd
        return None

    # 3 BIOS 三路径：普通全球站 / 中国站特供 / ROG（样本从刚构建的真实目录里挑）
    p("\n[3] BIOS 面板（三路径：prod 全球站 / prod 中国站 / ROG）")
    probes = (
        ("普通全球站", lambda bd: bd.get("src") == "prod" and bd.get("host") != "cn"),
        ("中国站特供", lambda bd: bd.get("src") == "prod" and bd.get("host") == "cn"),
        ("ROG", lambda bd: bd.get("src") == "rog"),
    )
    dl_probe = None
    for name, pred in probes:
        bd = _first_board(pred)
        if bd is None:
            p("  [%s] 目录中无此类型样本，跳过" % name)
            continue
        try:
            panel, host = api.get_bios_panel(bd, "bios")
            groups, _os, note, title = api.parse_bios_groups(panel or {})
            n = sum(len(v) for v in groups.values())
            p("  [%s] %s\n        slug=%s host=%s 分组=%s 共 %d 条" %
              (name, bd.get("title"), bd.get("slug"), host, list(groups.keys()), n))
            if dl_probe is None:
                for _b, items in groups.items():
                    for it in items:
                        if it.get("download_url"):
                            dl_probe = it
                            break
                    if dl_probe:
                        break
            for _b, items in list(groups.items())[:1]:
                for it in items[:2]:
                    p("        ver=%-10s date=%s size=%dB sha=%s url=%s" % (
                        it.get("download_version"), it.get("download_release"),
                        it.get("download_size"),
                        "Y" if parse_sha256_from_field(it.get("download_sha256")) else "N",
                        str(it.get("download_url"))[:88]))
        except Exception as e:
            p("  [%s] 面板异常: %r" % (name, e))

    # 4 下载直链连通性（dlcdnets.asus.com CDN，Range 探测）
    p("\n[4] 下载直链（dlcdnets.asus.com，HTTP Range）")
    if dl_probe is None:
        p("  无可用下载链接样本，跳过")
    else:
        try:
            u = dl_probe["download_url"]
            r = _cr_get_dns_safe(u, impersonate=IMPERSONATE, timeout=30,
                                 headers={"Range": "bytes=0-255", "User-Agent": UA_CHROME})
            p("  %s -> HTTP %s, 收到 %d 字节" % (u, r.status_code, len(r.content)))
            r.close()
        except Exception as e:
            p("  直链异常: %r" % (e,))

    p("\n自检结束")
    return 0


# ================================================================ main
# ---------------------------------------------------------------- DPI 说明（v2.0.0 审核）
# 本工具的字号与间距全部按「物理像素」标定（负值字号 = 像素，如 -17），视觉是在
# Windows **DPI 虚拟化**下校准的：进程不声明 DPI 感知 → 150% 缩放下 Windows 先按
# 96 DPI 渲染整窗、再放大 1.5×，于是 17px 字在屏幕上呈 25.5px，与设计一致。
# 若改为声明 Per-Monitor V2，Windows 不再放大位图，全部像素度量必须同步乘 DPI
# 系数，否则整体缩小 1/1.5 —— 属一次完整的度量重构，故此处显式保持现状。
# （旧版本这里有一个 _enable_dpi_awareness()，因 ctypes 作用域错误从未生效，
#   且 SetProcessDpiAwarenessContext 并不在 shcore.dll 中，属纯死代码，已移除。）


def main():
    if "--check" in sys.argv or "-c" in sys.argv:
        sys.exit(selftest())
    # v2.1.0：必须在创建 Tk 之前声明 DPI 感知 —— 否则 Tk 以 96 DPI 初始化，
    # 高缩放比下整个窗口被系统位图拉伸（文字发虚）。旧版把感知声明写成局部
    # ctypes 引用而从未生效，tk scaling 也错用了 DPI/96（应为 DPI/72）。
    aware = _init_dpi()
    try:
        # 先创建一个完整 Tk 走 tcl/tk 初始化，再创建 App
        root = tk.Tk()
        # 关键：构建期间窗口隐藏，构建完成再 deiconify 一次性显示完整 UI
        # —— 避免 Windows 在「Application Placeholder Window」阶段被截图
        root.withdraw()
        # 以窗口所在显示器的实际 DPI 校准（多屏混合 DPI 时比系统 DPI 更准）
        try:
            wdpi = int(ctypes.windll.user32.GetDpiForWindow(root.winfo_id()))
            if wdpi and wdpi != _DPI:
                _apply_dpi_scale(wdpi)
        except Exception:
            pass
        # tk scaling = 每磅像素数 = DPI/72（仅影响 pt 字号与少数控件内部度量；
        # 本程序字体为像素级负值，由 S() 缩放，不依赖此项）
        try:
            root.tk.call("tk", "scaling", _DPI / 72.0)
        except Exception:
            pass
        _log("dpi: aware=%s dpi=%d scale=%.3f work=%s" %
             (aware, _DPI, DPI_SCALE, _work_area()))
    except Exception as e:
        _log("tk init err: %r" % (e,))
        try:
            ctypes.windll.user32.MessageBoxW(0, "无法启动图形界面：%s" % e, APP_TITLE, 0x10)
        except Exception:
            pass
        sys.exit(1)
    try:
        App(root)
        root.update_idletasks()
        root.update()  # 先强制算一次 layout，让首帧一次画好
        # 让窗口出现在屏幕中央（必须 deiconify）
        root.deiconify()
        root.mainloop()
    except Exception as e:
        _log("app fatal: %r" % (e,))
        try:
            ctypes.windll.user32.MessageBoxW(0, "程序异常：%s" % e, APP_TITLE, 0x10)
        except Exception:
            pass


if __name__ == "__main__":
    main()
