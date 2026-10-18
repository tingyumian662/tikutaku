# Defects4J 本地运行环境（source 本文件后可跑 defects4j CLI）
# 用法: source tools/d4j_env.sh
# JDK 用 D 盘副本：项目树内 exe 被 Low 完整性标记拉低 → java 写不了 ~/.maven（2026-10-11 踩坑）
export JAVA_HOME="/d/tikutaku/jdk-11.0.32.1+1"
export PATH="$JAVA_HOME/bin:/e/AI/测试题库/sources/repos/defects4j/major/bin:$PATH"
export PERL5LIB="/e/AI/测试题库/vendor/perl5lib"
# Windows git 默认 CRLF 会破坏补丁应用，必须关掉（仅子进程生效，不改全局配置）
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.autocrlf GIT_CONFIG_VALUE_0=false
export D4J_HOME="/e/AI/测试题库/sources/repos/defects4j"
# 工作目录必须纯 ASCII 路径（ant 的 mkdir 在中文/短路径下会莫名失败）
export D4J_WORK="/e/AI/d4j_work"
alias d4j='perl "E:/AI/测试题库/sources/repos/defects4j/framework/bin/defects4j"'
