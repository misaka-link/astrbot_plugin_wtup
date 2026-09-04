const { createApp, ref, onMounted, computed, watch, nextTick } = Vue;

createApp({
  setup() {
    const currentTab = ref('dashboard');
    const mobileMenuOpen = ref(false);
    const tabs = [
      { id: 'dashboard', name: '监控大屏', icon: '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"></path></svg>' },
      { id: 'tasks', name: '任务与日志', icon: '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"></path></svg>' },
      { id: 'commits', name: 'GitHub 提交', icon: '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"></path></svg>' },
      { id: 'history', name: '历史报告', icon: '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>' },
      { id: 'manual', name: '手动触发', icon: '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z"></path><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>' },
      { id: 'settings', name: '系统设置', icon: '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"></path><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"></path></svg>' },
      { id: 'api_docs', name: 'API 文档', icon: '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 20l4-16m4 4l4 4-4 4M6 16l-4-4 4-4"></path></svg>' },
    ];

    const status = ref({});
    const task = ref({
      id: null,
      status: 'idle',
      status_text: '等待分析',
      stage: '等待分析',
      progress_percent: 0,
      logs: [],
      error: null,
      result_report_id: null,
      started_at: null,
      finished_at: null,
      trigger_mode: null,
      params: {},
      can_retry: true,
    });
    const retryingTask = ref(false);
    const showLogs = ref(true);
    const autoScrollLogs = ref(true);
    const logFilter = ref('all');
    const logSearch = ref('');
    const logNotice = ref('');

    const latestReport = ref(null);
    const latestReportImageUrl = ref('');
    const showImageModal = ref(false);
    const showHtmlModal = ref(false);
    const selectedTemplate = ref('discord');
    const errorMessage = ref('');

    const githubData = ref({ repo: '', branch: '', commits: [], last_checked_commit: '', source: '' });
    const loadingCommits = ref(false);
    const syncingGit = ref(false);

    const historyItems = ref([]);

    const settingsForm = ref({
      openai_base_url: 'https://api.openai.com/v1',
      openai_api_key: '',
      model: 'deepseek-chat',
      backup_models: [],
      summary_model: '',
      review_model: '',
      review_mode: 'auto',
      thinking_mode: 'off',
      thinking_budget_tokens: 0,
      temperature: 0.2,
      github_repo: 'gszabi99/War-Thunder-Datamine',
      github_branch: 'master',
      github_token: '',
      schedule_interval_minutes: 15,
      schedule_enabled: true,
      enable_struct_diff: true,
      enable_model_tool_calls: true,
      max_tool_call_rounds: 5,
      enable_dynamic_context_queue: true,
      render_template: 'discord',
      max_history_reports: 15,
      enable_ai_analysis: false,
      watermark_enabled: false,
      watermark_text: 'War Thunder Datamine',
      watermark_opacity: 0.12,
      watermark_size: 18,
      watermark_density: 'medium',
      render_scale: '1.5x',
      github_mobile_repo: 'gszabi99/War-Thunder-Mobile-Datamine',
      github_mobile_branch: 'master',
      mobile_schedule_interval_minutes: 15,
      mobile_schedule_enabled: true,
      mobile_watermark_enabled: false,
      mobile_watermark_text: 'War Thunder Mobile Datamine',
      mobile_watermark_opacity: 0.12,
      mobile_watermark_size: 18,
      mobile_watermark_density: 'medium',
    });
    const activeTarget = ref('pc');
    const activeWatermarkTab = ref('pc');
    const availableModels = ref([
      'deepseek-chat', 'deepseek-reasoner',
      'gpt-4o', 'gpt-4o-mini', 'o1-mini', 'o3-mini',
      'qwen-max', 'qwen-plus', 'claude-3-7-sonnet',
    ]);
    const loadingModels = ref(false);

    const manualForm = ref({
      mode: 'latest',
      compare_range: '',
      diff_text: '',
    });

    // 格式化日期时间 (统一采用北京时间 UTC+8)
    const formatDate = (isoStr) => {
      if (!isoStr) return '-';
      try {
        const d = new Date(isoStr);
        return d.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false });
      } catch (e) {
        return isoStr;
      }
    };

    // 本地时钟心跳 (供倒计时动态响应)
    const currentTime = ref(Date.now());
    setInterval(() => {
      currentTime.value = Date.now();
    }, 1000);

    // 计算下一次查询时间与友好倒计时文本
    const nextCheckTimeDisplay = computed(() => {
      const now = currentTime.value;
      if (!status.value?.schedule_enabled) {
        return '未启用定时';
      }
      if (task.value?.status === 'running') {
        return '正在查询...';
      }
      const iso = status.value?.next_check_at;
      if (!iso) {
        return '即将检查';
      }
      try {
        const target = new Date(iso).getTime();
        if (isNaN(target)) return '-';
        const d = new Date(iso);
        const timeStr = d.toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false });
        const diffSec = Math.round((target - now) / 1000);

        if (diffSec <= 0) {
          return `${timeStr} (即将查询)`;
        }
        if (diffSec < 60) {
          return `${timeStr} (${diffSec}秒后)`;
        }
        const diffMin = Math.floor(diffSec / 60);
        if (diffMin < 60) {
          const remainSec = diffSec % 60;
          return remainSec > 0 ? `${timeStr} (${diffMin}分${remainSec}秒后)` : `${timeStr} (${diffMin}分后)`;
        }
        const diffHour = Math.floor(diffMin / 60);
        const remainMin = diffMin % 60;
        return `${timeStr} (${diffHour}小时${remainMin}分后)`;
      } catch (e) {
        return iso;
      }
    });

    // 计算任务执行耗时
    const taskDuration = computed(() => {
      const start = task.value?.started_at;
      if (!start) return '-';
      const end = task.value?.finished_at ? new Date(task.value.finished_at) : new Date();
      const diffMs = end.getTime() - new Date(start).getTime();
      if (isNaN(diffMs) || diffMs < 0) return '-';
      const sec = Math.floor(diffMs / 1000);
      if (sec < 60) return sec + 's';
      const min = Math.floor(sec / 60);
      return min + 'm ' + (sec % 60) + 's';
    });

    // 过滤与筛选任务日志
    const filteredLogs = computed(() => {
      const logs = task.value?.logs || [];
      return logs.filter((line) => {
        if (!line) return false;
        if (logFilter.value === 'error') {
          if (!line.includes('[ERROR]') && !line.includes('失败') && !line.includes('异常') && !line.includes('Error') && !line.includes('Exception')) {
            return false;
          }
        } else if (logFilter.value === 'warn') {
          if (!line.includes('[WARNING]') && !line.includes('[WARN]')) return false;
        } else if (logFilter.value === 'info') {
          if (!line.includes('[INFO]')) return false;
        }
        if (logSearch.value.trim()) {
          const q = logSearch.value.trim().toLowerCase();
          if (!line.toLowerCase().includes(q)) return false;
        }
        return true;
      });
    });

    // 日志条目高亮样式判定
    const getLogLineClass = (line) => {
      if (!line) return 'text-slate-300';
      if (line.includes('[ERROR]') || line.includes('失败') || line.includes('异常') || line.includes('Error') || line.includes('Exception')) {
        return 'text-rose-400 font-medium';
      }
      if (line.includes('[WARNING]') || line.includes('[WARN]')) {
        return 'text-amber-300 font-medium';
      }
      if (line.includes('=== 任务') || line.includes('分析完成') || line.includes('成功')) {
        return 'text-emerald-400 font-semibold';
      }
      if (line.includes('[Git') || line.includes('[GitRepoManager]') || line.includes('git ls-remote')) {
        return 'text-indigo-300';
      }
      return 'text-slate-300';
    };

    // 获取系统状态与当前任务 (支持 target=pc 或 target=mobile)
    const fetchStatus = async (target) => {
      const t = target || activeTarget.value;
      try {
        const res = await fetch('/api/status?target=' + t);
        if (res.ok) {
          const data = await res.json();
          status.value = data;
          if (data.current_task) {
            const prevStatus = task.value.status;
            task.value = data.current_task;
            // 只有当任务成功完成 (completed) 时才去拉取最新报告！
            if (prevStatus === 'running' && data.current_task.status === 'completed') {
              fetchLatest(t);
              fetchHistory(t);
            }
          }
        }
      } catch (e) {
        console.error('fetchStatus failed:', e);
      }
    };

    // 切换端游与手游通道
    const switchTarget = (t) => {
      activeTarget.value = t;
      fetchStatus(t);
      fetchLatest(t);
      fetchHistory(t);
      fetchCommits(false, t);
    };

    // 监听日志变化自动滚动到底部
    watch(
      () => task.value?.logs?.length,
      () => {
        if (autoScrollLogs.value) {
          nextTick(() => {
            const els = document.querySelectorAll('.task-log-terminal');
            els.forEach((el) => {
              el.scrollTop = el.scrollHeight;
            });
          });
        }
      }
    );

    // 重试任务
    const retryTask = async () => {
      if (task.value.status === 'running' || retryingTask.value) return;
      retryingTask.value = true;
      try {
        const res = await fetch('/api/analyze/retry', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ target: activeTarget.value }),
        });
        if (res.ok) {
          const data = await res.json();
          if (data.task) {
            task.value = data.task;
          }
          logNotice.value = '已重新发起分析任务！';
          setTimeout(() => { logNotice.value = ''; }, 3000);
          fetchStatus();
        } else {
          const err = await res.json();
          errorMessage.value = err.error || '重试失败';
        }
      } catch (e) {
        errorMessage.value = '请求重试失败: 无法连接服务器';
      } finally {
        retryingTask.value = false;
      }
    };

    // 清空日志
    const clearLogs = async () => {
      try {
        const res = await fetch('/api/task/clear-logs', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ target: activeTarget.value }),
        });
        if (res.ok) {
          if (task.value) task.value.logs = [];
          logNotice.value = '日志已清空';
          setTimeout(() => { logNotice.value = ''; }, 2000);
        }
      } catch (e) {
        console.error('clearLogs failed:', e);
      }
    };

    // 复制全部日志到剪贴板
    const copyLogs = () => {
      const logs = task.value?.logs || [];
      if (!logs.length) {
        alert('当前暂无日志可复制');
        return;
      }
      navigator.clipboard.writeText(logs.join('\n')).then(() => {
        logNotice.value = '全部日志已复制到剪贴板！';
        setTimeout(() => { logNotice.value = ''; }, 2500);
      }).catch(() => {
        alert('复制失败，请手动选取复制');
      });
    };

    // 下载日志为文本文件
    const downloadLogs = () => {
      const logs = task.value?.logs || [];
      if (!logs.length) {
        alert('当前暂无日志可供下载');
        return;
      }
      const text = logs.join('\n');
      const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = (task.value.id || 'task') + '_logs.txt';
      a.click();
      URL.revokeObjectURL(url);
    };

    // 获取最新报告 (支持 target=pc 或 target=mobile)
    const fetchLatest = async (target) => {
      const t = target || activeTarget.value;
      try {
        const res = await fetch('/api/latest?target=' + t);
        if (res.ok) {
          const data = await res.json();
          if (data.report) {
            latestReport.value = data.report;
            latestReportImageUrl.value = data.image_url || '';
          } else {
            latestReport.value = null;
            latestReportImageUrl.value = '';
          }
        }
      } catch (e) {
        console.error('fetchLatest failed:', e);
      }
    };

    // 获取 GitHub 提交记录 (支持 target=pc 或 target=mobile)
    const fetchCommits = async (forceSync = false, target) => {
      const t = target || activeTarget.value;
      loadingCommits.value = true;
      try {
        const url = '/api/github/commits?limit=25&target=' + t + (forceSync ? '&refresh=1' : '');
        const res = await fetch(url);
        if (res.ok) {
          const data = await res.json();
          githubData.value = data;
        } else {
          const err = await res.json();
          errorMessage.value = err.error || '获取提交失败';
        }
      } catch (e) {
        errorMessage.value = '连接 GitHub 服务异常';
      } finally {
        loadingCommits.value = false;
      }
    };

    // 同步本地 Git 仓库 (支持 target=pc 或 target=mobile)
    const syncLocalGit = async () => {
      syncingGit.value = true;
      try {
        const res = await fetch('/api/git/sync', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ target: activeTarget.value }),
        });
        if (res.ok) {
          const data = await res.json();
          if (data.success) {
            alert('本地 Git 仓库同步完成！已切换为本地毫秒级零限流模式。');
            fetchCommits(false, activeTarget.value);
          } else {
            alert('Git 同步失败，请检查网络连接或 Git 目录配置。');
          }
        }
      } catch (e) {
        alert('无法连接服务器执行 Git 同步');
      } finally {
        syncingGit.value = false;
      }
    };

    // 获取历史报告列表 (支持 target=pc 或 target=mobile)
    const fetchHistory = async (target) => {
      const t = target || activeTarget.value;
      try {
        const res = await fetch('/api/history?target=' + t);
        if (res.ok) {
          const data = await res.json();
          historyItems.value = data.items || [];
        }
      } catch (e) {
        console.error('fetchHistory failed:', e);
      }
    };

    // 获取系统设置
    const fetchSettings = async () => {
      try {
        const res = await fetch('/api/config');
        if (res.ok) {
          const data = await res.json();
          Object.assign(settingsForm.value, data);
        }
      } catch (e) {
        console.error('fetchSettings failed:', e);
      }
    };

    // 保存系统设置
    const saveSettings = async () => {
      try {
        const res = await fetch('/api/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(settingsForm.value),
        });
        if (res.ok) {
          alert('配置已成功保存！');
          fetchStatus();
        } else {
          const err = await res.json();
          alert('保存失败: ' + (err.error || '未知错误'));
        }
      } catch (e) {
        alert('保存失败: 无法连接服务器');
      }
    };

    // 从网关动态拉取模型列表
    const fetchRemoteModels = async () => {
      loadingModels.value = true;
      try {
        const res = await fetch('/api/models/fetch', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            base_url: settingsForm.value.openai_base_url,
            api_key: settingsForm.value.openai_api_key,
          }),
        });
        if (res.ok) {
          const data = await res.json();
          if (data.models && data.models.length) {
            availableModels.value = data.models;
            alert('成功拉取到 ' + data.models.length + ' 个可用模型！');
          } else {
            alert('未从该接口返回可用模型列表。');
          }
        } else {
          const err = await res.json();
          alert('获取模型失败: ' + (err.error || '未知原因'));
        }
      } catch (e) {
        alert('网络请求失败，请检查 Base URL 与 Key 是否有效');
      } finally {
        loadingModels.value = false;
      }
    };

    // 快捷触发最新检查 (带目标参数)
    const triggerQuickCheck = async () => {
      if (task.value.status === 'running') return;
      try {
        const res = await fetch('/api/analyze/trigger', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ mode: 'latest', target: activeTarget.value }),
        });
        if (res.ok) {
          const data = await res.json();
          if (data.task) task.value = data.task;
          fetchStatus();
        } else {
          const err = await res.json();
          errorMessage.value = err.error || '触发失败';
        }
      } catch (e) {
        errorMessage.value = '无法连接服务器';
      }
    };

    // 提交手动分析任务 (带目标参数)
    const submitManualAnalysis = async () => {
      if (task.value.status === 'running') return;
      try {
        const payload = { mode: manualForm.value.mode, target: activeTarget.value };
        if (manualForm.value.mode === 'compare') {
          payload.compare_range = manualForm.value.compare_range;
        } else if (manualForm.value.mode === 'diff') {
          payload.diff_text = manualForm.value.diff_text;
        }

        const res = await fetch('/api/analyze/trigger', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        if (res.ok) {
          const data = await res.json();
          if (data.task) task.value = data.task;
          currentTab.value = 'dashboard';
          fetchStatus();
        } else {
          const err = await res.json();
          errorMessage.value = err.error || '任务启动失败';
        }
      } catch (e) {
        errorMessage.value = '请求发送失败';
      }
    };

    // 快捷从提交列表发起比对
    const quickCompareToLatest = (sha) => {
      manualForm.value.mode = 'compare';
      const headSha = githubData.value.commits[0]?.sha || 'HEAD';
      manualForm.value.compare_range = sha + '...' + headSha;
      currentTab.value = 'manual';
    };

    // 文件上传处理
    const handleFileUpload = (e) => {
      const file = e.target.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = (evt) => {
        manualForm.value.diff_text = evt.target.result;
      };
      reader.readAsText(file);
    };

    // 任务状态中文统一显示 (分析中 / 分析完成 / 分析失败 / 等待分析)
    const statusDisplay = computed(() => {
      if (task.value?.status === 'running') return '分析中';
      if (task.value?.status === 'completed') return '分析完成';
      if (task.value?.status === 'failed') return '分析失败';
      return '等待分析';
    });

    // 删除单份历史分析报告
    const deleteReport = async (id) => {
      if (!confirm('确定要删除这份历史分析报告及对应图片吗？此操作无法撤销。')) return;
      try {
        const res = await fetch('/api/history/' + id, { method: 'DELETE' });
        if (res.ok) {
          historyItems.value = historyItems.value.filter((item) => item.id !== id);
          if (latestReport.value && latestReport.value.id === id) {
            fetchLatest();
          }
          logNotice.value = '历史报告已删除';
          setTimeout(() => { logNotice.value = ''; }, 2500);
        } else {
          const err = await res.json();
          alert('删除失败: ' + (err.error || '未知错误'));
        }
      } catch (e) {
        console.error('deleteReport failed:', e);
      }
    };

    // 查看单份历史报告详情
    const viewReportDetail = async (id) => {
      try {
        const res = await fetch('/api/history/' + id);
        if (res.ok) {
          const data = await res.json();
          latestReport.value = data.report;
          latestReportImageUrl.value = data.image_url || '';
          currentTab.value = 'dashboard';
          window.scrollTo({
            top: 0,
            behavior: 'smooth'
          });
        }
      } catch (e) {
        console.error('viewReportDetail failed:', e);
      }
    };

    // 直接在弹窗中打开 HTML 渲染报告预览
    const openReportHtml = async (id) => {
      if (!id) return;
      await viewReportDetail(id);
      showHtmlModal.value = true;
    };

    const imageTimestamp = ref(Date.now());
    const rerenderingImage = ref(false);

    // 水印实时样式预览 (SVG 矢量平铺背景，支持端游/手游自由切换预览)
    const watermarkPreviewStyle = computed(() => {
      const isMobile = (activeWatermarkTab.value === 'mobile');
      const text = (isMobile ? settingsForm.value.mobile_watermark_text : settingsForm.value.watermark_text) || (isMobile ? 'War Thunder Mobile Datamine' : 'War Thunder Datamine');
      const opacity = (isMobile ? settingsForm.value.mobile_watermark_opacity : settingsForm.value.watermark_opacity) ?? 0.12;
      const size = (isMobile ? settingsForm.value.mobile_watermark_size : settingsForm.value.watermark_size) ?? 18;
      const density = (isMobile ? settingsForm.value.mobile_watermark_density : settingsForm.value.watermark_density) || 'medium';
      const densityMap = { high: [180, 120], medium: [260, 180], low: [380, 260] };
      const [w, h] = densityMap[density] || [260, 180];
      const color = `rgba(255, 255, 255, ${opacity})`;
      const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}"><text x="${w/2}" y="${h/2}" text-anchor="middle" dominant-baseline="middle" transform="rotate(-25 ${w/2} ${h/2})" fill="${color}" font-size="${size}px" font-family="-apple-system, BlinkMacSystemFont, sans-serif" font-weight="600" letter-spacing="1px">${text}</text></svg>`;
      return {
        backgroundImage: `url("data:image/svg+xml;utf8,${encodeURIComponent(svg)}")`,
        backgroundRepeat: 'repeat',
      };
    });

    const currentHtmlUrl = computed(() => {
      if (!latestReport.value) return '';
      let url = '/api/report-html/' + latestReport.value.id + '?template=' + selectedTemplate.value;
      const isMobile = (activeTarget.value === 'mobile');
      const wmEnabled = isMobile ? settingsForm.value.mobile_watermark_enabled : settingsForm.value.watermark_enabled;
      const wmText = isMobile ? settingsForm.value.mobile_watermark_text : settingsForm.value.watermark_text;
      const wmOpacity = isMobile ? settingsForm.value.mobile_watermark_opacity : settingsForm.value.watermark_opacity;
      const wmSize = isMobile ? settingsForm.value.mobile_watermark_size : settingsForm.value.watermark_size;
      const wmDensity = isMobile ? settingsForm.value.mobile_watermark_density : settingsForm.value.watermark_density;
      if (wmEnabled) {
        url += '&watermark=1';
        if (wmText) url += '&wm_text=' + encodeURIComponent(wmText);
        if (wmOpacity != null) url += '&wm_opacity=' + wmOpacity;
        if (wmSize != null) url += '&wm_size=' + wmSize;
        if (wmDensity) url += '&wm_density=' + wmDensity;
      } else {
        url += '&watermark=0';
      }
      return url;
    });

    // 重新渲染报告图片 (按最新配置、清晰度档位与水印重新渲染)
    const rerenderReportImage = async (reportId) => {
      if (!reportId) return;
      rerenderingImage.value = true;
      const isMobile = (activeTarget.value === 'mobile');
      try {
        const res = await fetch('/api/reports/' + reportId + '/rerender', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            template: selectedTemplate.value,
            render_scale: settingsForm.value.render_scale,
            watermark_enabled: isMobile ? settingsForm.value.mobile_watermark_enabled : settingsForm.value.watermark_enabled,
            watermark_text: isMobile ? settingsForm.value.mobile_watermark_text : settingsForm.value.watermark_text,
            watermark_opacity: isMobile ? settingsForm.value.mobile_watermark_opacity : settingsForm.value.watermark_opacity,
            watermark_size: isMobile ? settingsForm.value.mobile_watermark_size : settingsForm.value.watermark_size,
            watermark_density: isMobile ? settingsForm.value.mobile_watermark_density : settingsForm.value.watermark_density,
          })
        });
        if (res.ok) {
          imageTimestamp.value = Date.now();
          logNotice.value = '图片已按最新清晰度档位与水印重新渲染完成！';
          setTimeout(() => { logNotice.value = ''; }, 3000);
        } else {
          const err = await res.json();
          alert('重新渲染失败: ' + (err.error || '未知错误'));
        }
      } catch (e) {
        alert('重新渲染异常: ' + e);
      } finally {
        rerenderingImage.value = false;
      }
    };
    const openHtmlModal = () => {
      showHtmlModal.value = true;
    };

    // 打开大图模态框
    const openImageModal = () => {
      showImageModal.value = true;
    };

    // 预览单张资产图片
    const previewSingleImage = (url) => {
      latestReportImageUrl.value = url;
      showImageModal.value = true;
    };

    // 下载 Markdown 报告
    const downloadMarkdown = (report) => {
      if (!report) return;
      let md = '# ' + (report.report_title || 'War Thunder Datamine 更新报告') + '\n\n';
      md += '**重要程度**: ' + (report.importance || '中') + '\n\n';
      if (report.tags && report.tags.length) {
        md += '**标签**: ' + report.tags.join(' · ') + '\n\n';
      }
      md += '**更新概述**:\n' + report.summary + '\n\n';

      const data = report.data || {};
      if (data.update_sections) {
        md += '## 更新详情\n\n';
        for (const sec of data.update_sections) {
          md += '### ' + sec.title + '\n\n';
          for (const it of sec.items || []) {
            const text = it.text || it;
            md += '- ' + text + '\n';
            for (const c of it.children || []) {
              md += '  * ' + (c.text || c) + '\n';
            }
          }
          md += '\n';
        }
      }

      const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = (report.report_title || 'report') + '.md';
      a.click();
      URL.revokeObjectURL(url);
    };

    // 轮询定时器
    onMounted(() => {
      fetchStatus();
      fetchLatest();
      fetchSettings();
      fetchHistory();
      fetchCommits();

      setInterval(() => {
        fetchStatus();
      }, 2500);
    });

    return {
      currentTab,
      mobileMenuOpen,
      tabs,
      status,
      task,
      retryingTask,
      showLogs,
      autoScrollLogs,
      logFilter,
      logSearch,
      logNotice,
      filteredLogs,
      taskDuration,
      getLogLineClass,
      retryTask,
      clearLogs,
      copyLogs,
      downloadLogs,
      deleteReport,
      statusDisplay,
      nextCheckTimeDisplay,
      openReportHtml,
      latestReport,
      latestReportImageUrl,
      showImageModal,
      errorMessage,
      githubData,
      loadingCommits,
      syncingGit,
      syncLocalGit,
      historyItems,
      settingsForm,
      availableModels,
      loadingModels,
      manualForm,
      formatDate,
      fetchStatus,
      fetchLatest,
      fetchCommits,
      fetchHistory,
      fetchSettings,
      saveSettings,
      fetchRemoteModels,
      triggerQuickCheck,
      submitManualAnalysis,
      quickCompareToLatest,
      handleFileUpload,
      viewReportDetail,
      selectedTemplate,
      showHtmlModal,
      currentHtmlUrl,
      openHtmlModal,
      openImageModal,
      previewSingleImage,
      downloadMarkdown,
      imageTimestamp,
      rerenderingImage,
      watermarkPreviewStyle,
      rerenderReportImage,
      activeTarget,
      activeWatermarkTab,
      switchTarget,
    };
  }
}).mount('#app');
