# 数据自动更新管道

这套东西的作用：**每半个月自动抓一次榜单，生成 data.json，App 打开时自动读取。** 配一次，之后不用管。

技术栈：GitHub Actions（定时跑）+ Python（抓取）+ LLM（结构化）+ GitHub Pages（托管）。全部免费。

---

## 它怎么工作

```
每 3 天 凌晨 2 点
        ↓
GitHub Actions 启动一台 Ubuntu
        ↓
fetch_data.py 抓取榜单文章 → 交给 LLM 认剧名、归题材
        ↓
聚合回题材结构，写入 data.json
        ↓
git commit + push
        ↓
GitHub Pages 自动发布 → https://你.github.io/仓库名/data.json
        ↓
App 打开时 fetch 这个地址，覆盖本地数据
```

关键设计：**LLM 只负责「认剧名、归题材」这种它擅长的事**，`id`、`alias`、`naming`、`edge` 这些结构字段一律不动。所以模型每次输出都稳定，不会因为一次胡编就毁掉整个库。

---

## 五步配置

### 1. 建仓库

GitHub 新建一个 public 仓库（Pages 免费版要求 public）。

把 `manju-radar` 整个目录的内容传到仓库根目录。传完应该是这样：

```
你的仓库/
├── index.html
├── data.json            ← 第一次跑完才会有
├── auto/
│   ├── baseline.json
│   ├── fetch_data.py
│   └── requirements.txt
└── .github/
    └── workflows/
        └── update.yml
```

### 2. 开 Pages

仓库 Settings → Pages → Source 选 `Deploy from a branch` → Branch 选 `main`，目录选 `/ (root)` → Save。

等一两分钟，会给你一个地址，形如 `https://你的用户名.github.io/仓库名/`。

### 3. 配置 Secrets

仓库 Settings → Secrets and variables → Actions → New repository secret，加四个：

| 名字 | 填什么 | 必填 |
|---|---|---|
| `SOURCE_URLS` | 榜单文章地址，多个用英文逗号分隔 | 是 |
| `LLM_KEY` | 模型 API Key | 是 |
| `LLM_BASE` | 接口地址，如 `https://api.deepseek.com/v1` | 否 |
| `LLM_MODEL` | 模型名，如 `deepseek-chat` | 否 |

`SOURCE_URLS` 填什么：DataEye 每月发的《AI 剧漫剧百强榜》文章地址。这类文章通常发在澎湃号、电影界、微信公众号转载页。**要填网页地址，不是公众号文章的内部链接**——脚本需要能直接 HTTP 抓到的 URL。

### 4. 手动跑一次

仓库 Actions 标签 → 左边选「更新漫剧数据」→ 右边 Run workflow。

跑完看日志：应该能看到「抓到 XXXX 字」「模型抽出 N 部作品」「已写入 data.json」。

如果第一次就成功，`data.json` 会出现在仓库根目录，Pages 地址就能访问了。

### 5. 连回 App

打开 `index.html` → 雷达页点数据日期那行 → 数据源页 → 远程数据源填：

```
https://你的用户名.github.io/仓库名/data.json
```

点「立即拉取」，看到「已更新到 2026-XX-XX」就成了。以后每次打开 App 都会自动同步。

---

## 防止它写坏数据

脚本有四道闸：

1. **抓不到内容就退出**，不写文件 —— Action 会标红提醒你
2. **模型抽出的作品少于 5 部就退出** —— 说明抓到的可能不是榜单页
3. **只覆盖 count / heat / samples / trend** —— 其余字段原样保留
4. **数据没实质变化就不写文件、不改进日期** —— 这条最关键

第 4 条要展开说一下。榜单文章是月更的，但脚本 3 天跑一次。如果每次都把 `updated` 刷成今天，就会出现「日期看着很新、数据其实是一个月前的」——展示上的新鲜度就变成谎言了，而这恰恰是这个产品最不该骗人的地方。

所以脚本每次会跟上一期的 `genres` 逐字段对比，完全一致就直接退出，日期原样保留。**只有真有新作品进来，日期才会动。**

所以最坏的情况是「这次没更新」，不会是「数据被写烂」。

---

## 可能失败的地方

**抓取被拦**：部分站点有反爬。表现是日志里「抓到 0 字」或直接超时。换一个转载源试试——同一篇榜单文章通常有好几个站点在转。

**文章改版**：脚本靠通用 HTML 解析取正文，不依赖具体 DOM 结构，所以改版影响不大。但如果站点改成纯图片发布（榜单做成图片），就抓不到了，这是硬限制。

**模型归错题材**：概率不高，但如果某部剧被分错，那次的 count 会偏。检查 data.json 里的 samples 能看出来。发现长期偏，就在 `fetch_data.py` 的 `GENRE_HINT` 里给那个题材补几个关键词。

**Pages 没生效**：新仓库第一次开 Pages 要等几分钟，别急着判断失败。

---

## 成本

- GitHub Actions：免费额度 2000 分钟/月，这个任务一次 1—2 分钟，每月两次
- LLM：一次约 1—2 万 token，DeepSeek 大概 1—3 分钱
- Pages：免费

一年成本大约 5 毛钱。

---

## 一个替代方案（不想用 GitHub）

如果你有服务器或者云函数，把 `fetch_data.py` 挂上去，定时触发，输出到任意支持 CORS 的对象存储（七牛、阿里云 OSS、腾讯云 COS 都行），App 里填那个地址即可。逻辑完全一样，只是把「GitHub Actions」换成「你的定时任务」。

---

## 面试时怎么讲这块

不要说「我做了自动更新」。说这个：

> 「行业榜单没有 API，所以我搭了一条管道：GitHub Actions 定时抓文章，用模型把非结构化的榜单转成 JSON，托管到 Pages，前端每次打开自动拉。设计上我刻意让模型只做抽取不做生成——结构字段写死，这样模型出错最多是某一期数据不准，不会把整个库搞坏。」

这段话里有三个产品判断：**知道真正的瓶颈在哪、知道怎么把风险收在局部、知道降级路径**。比「我做了个爬虫」值钱得多。
