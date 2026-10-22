# vidcut 常用命令。运行 `make` 或 `make help` 查看说明。
#
# 可覆盖的变量（写在 make 后面，例如 make auto SRC=~/Movies/旅行 NAME="我的旅行" WORK=work_mytrip）：
WORK  ?= work
SRC   ?=
NAME  ?=
PORT  ?= 8766
MODEL ?=

# 有 uv 就用它（自动装依赖）；Docker 镜像里依赖已装好，直接用 python3
RUN    ?= $(if $(shell command -v uv 2>/dev/null),uv run,python3)
VIDCUT := $(RUN) vidcut.py -w "$(WORK)"
SRC_ARG   := $(if $(SRC),"$(SRC)")
NAME_ARG  := $(if $(NAME),--name "$(NAME)")
MODEL_ARG := $(if $(MODEL),--model "$(MODEL)")

.DEFAULT_GOAL := help
.PHONY: help samples auto final rebuild update scan proxy analyze transcribe translate transcript \
        summarize roughcut preview render open web web-php status clean-segments

help: ## 显示这份说明
	@echo "用法：make <目标> [WORK=工作目录] [SRC=素材目录] [NAME=片名] [PORT=端口] [MODEL=Whisper模型]"
	@echo "当前：WORK=$(WORK)  SRC=$(or $(SRC),（未指定，用以前扫描过的目录）)  PORT=$(PORT)"
	@echo
	@echo "全自动："
	@grep -E '^[a-z-]+:.*?## \[自动\]' $(MAKEFILE_LIST) | sed -E 's/:.*## \[自动\] ?/\t/' | awk -F'\t' '{printf "  make %-14s %s\n", $$1, $$2}'
	@echo
	@echo "分步（按顺序）："
	@grep -E '^[a-z-]+:.*?## \[步骤\]' $(MAKEFILE_LIST) | sed -E 's/:.*## \[步骤\] ?/\t/' | awk -F'\t' '{printf "  make %-14s %s\n", $$1, $$2}'
	@echo
	@echo "编辑和查看："
	@grep -E '^[a-z-]+:.*?## \[其他\]' $(MAKEFILE_LIST) | sed -E 's/:.*## \[其他\] ?/\t/' | awk -F'\t' '{printf "  make %-14s %s\n", $$1, $$2}'
	@echo
	@echo "例子："
	@echo "  make auto SRC=~/Movies/日本 NAME=\"日本之旅\" WORK=work_japan   # 一条命令出预览视频"
	@echo "  make web WORK=work_japan                                     # 网页里调整"
	@echo "  make preview WORK=work_japan                                 # 改完重出预览"

samples: ## [其他] 生成 6 段演示用的样例视频到 samples/
	./make_samples.sh samples

# ───── 全自动 ─────

auto: ## [自动] 素材目录 → 分析、转录、翻译、初剪 → 预览视频（保留已有的 project.json）
	$(VIDCUT) auto $(SRC_ARG) $(NAME_ARG) $(MODEL_ARG)

final: ## [自动] 同 auto，并且同时渲染 1080p 成片
	$(VIDCUT) auto $(SRC_ARG) $(NAME_ARG) $(MODEL_ARG) --final

rebuild: ## [自动] 按 settings.json 重新生成剪辑并出预览（会覆盖 project.json 的手动修改）
	$(VIDCUT) auto $(SRC_ARG) $(NAME_ARG) $(MODEL_ARG) --rebuild

update: ## [自动] 加入新素材：重新扫描目录，只处理新增视频，不改 project.json
	$(VIDCUT) update $(SRC_ARG) $(MODEL_ARG)

# ───── 分步 ─────

scan: ## [步骤] 1 扫描素材目录 → media.json（需要 SRC=目录）
	@test -n "$(SRC)" || { echo "请指定素材目录：make scan SRC=目录"; exit 1; }
	$(VIDCUT) scan "$(SRC)"

proxy: ## [步骤] 2 生成代理文件和缩略图
	$(VIDCUT) proxy

analyze: ## [步骤] 3 检测静音、黑屏、场景切换，抽关键帧
	$(VIDCUT) analyze

transcribe: ## [步骤] 4 语音转录（自动识别中文/英文）
	$(VIDCUT) transcribe $(MODEL_ARG)

translate: ## [步骤] 5 翻译语音（中文→英文，英文→中文）
	$(VIDCUT) translate

summarize: ## [步骤] 6 AI 摘要与初剪建议
	$(VIDCUT) summarize

roughcut: ## [步骤] 7 按 settings.json 生成 project.json（会覆盖已有的）
	$(VIDCUT) roughcut --force $(NAME_ARG)

preview: ## [步骤] 8 渲染预览视频（低分辨率，只重渲改过的段落）
	$(VIDCUT) render --draft

render: ## [步骤] 9 渲染 1080p 成片
	$(VIDCUT) render

# ───── 编辑和查看 ─────

transcript: ## [其他] 把所有语音（含译文）整理成 transcript.md 并显示
	$(VIDCUT) transcript

web: ## [其他] 启动网页编辑器（Python 版），浏览器打开 http://127.0.0.1:端口（默认 8766）
	python3 web.py -w "$(WORK)" --port $(PORT)

web-php: ## [其他] 启动网页编辑器（PHP 版）
	VIDCUT_WORK="$(WORK)" PHP_CLI_SERVER_WORKERS=4 php -S 127.0.0.1:$(PORT) index.php

open: ## [其他] 打开预览视频和输出目录
	@ls "$(WORK)"/out/*_preview.mp4 >/dev/null 2>&1 || { echo "还没有预览视频，先运行 make auto 或 make preview"; exit 1; }
	open "$$(ls -t "$(WORK)"/out/*_preview.mp4 | head -1)"
	open "$(WORK)/out"

status: ## [其他] 查看工作目录里各步骤的进度
	@test -f "$(WORK)/media.json" || { echo "$(WORK) 还没有扫描过素材"; exit 0; }
	@n=$$(ls -d "$(WORK)"/derived/m_* 2>/dev/null | wc -l | tr -d ' '); \
	 for f in proxy.mp4 analysis.json transcript.json summary.json; do \
	   c=$$(ls "$(WORK)"/derived/m_*/$$f 2>/dev/null | wc -l | tr -d ' '); printf "  %-16s %s/%s\n" $$f $$c $$n; \
	 done
	@for f in settings.json project.json transcript.md; do \
	   if [ -f "$(WORK)/$$f" ]; then printf "  %-16s 有\n" $$f; else printf "  %-16s 无\n" $$f; fi; done
	@ls -lh "$(WORK)"/out/*.mp4 2>/dev/null | awk '{print "  " $$NF "  " $$5}' || true

clean-segments: ## [其他] 删除渲染缓存（derived/segments），下次渲染全部重来；不动素材和项目
	rm -rf "$(WORK)/derived/segments"
