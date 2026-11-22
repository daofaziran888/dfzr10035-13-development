<template>
  <section class="page" data-module="collection">
    <header class="page-head">
      <div>
        <h2>采集处理 · 分时补采</h2>
        <p class="page-desc">
          按端点缺采时段的实际覆盖切分成分时补采批次，一次提交多条申请；成功覆盖只入账一次，
          每份申请都能回看实际补了哪些区间。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="collection-refresh" type="button" @click="reload">刷新</button>
        <button
          v-if="endpoint"
          class="btn"
          data-testid="collection-resume"
          type="button"
          :disabled="busy"
          @click="resume"
        >
          核对回执并恢复
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="analysis-form" data-testid="collection-form" @submit.prevent="submit">
      <label>
        <span>关联端点</span>
        <input v-model="form.endpoint" data-testid="collection-endpoint" placeholder="ENDP-CSV-0001" required />
      </label>
      <label>
        <span>补采日期</span>
        <input v-model="form.date" data-testid="collection-date" placeholder="2026-11-22（缺省当天）" />
      </label>
      <label>
        <span>补采时段（每行一条，如 08:00-10:00）</span>
        <textarea
          v-model="form.windows"
          data-testid="collection-windows"
          rows="3"
          placeholder="08:00-10:00&#10;09:00-11:00"
        />
      </label>
      <button class="btn primary" data-testid="collection-submit" type="submit" :disabled="busy">
        {{ busy ? '提交中…' : '提交补采申请' }}
      </button>
    </form>

    <p v-if="message" class="status-legend" data-testid="collection-message">{{ message }}</p>
    <p v-if="errorMessage" class="error-text" data-testid="collection-error">{{ errorMessage }}</p>

    <h3 class="section-title">分时补采批次</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>批次编号</th><th>时段</th><th>覆盖申请</th><th>状态</th><th>补采结论</th>
          <th>已覆盖</th><th>未覆盖</th><th>请求批次</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="batch in batches" :key="batch.id" :data-testid="`collection-batch-${batch.批次编号}`">
          <td>{{ batch.批次编号 }}</td>
          <td>{{ batch.时段 }}</td>
          <td>{{ batch.覆盖申请.join('、') }}</td>
          <td>{{ batch.状态 }}</td>
          <td>{{ batch.补采结论 }}</td>
          <td>{{ batch.已覆盖时段 }}</td>
          <td>{{ batch.未覆盖时段 }}</td>
          <td>{{ batch.请求批次编号 || '—' }}</td>
          <td class="row-actions">
            <button
              v-if="canDispatch(batch)"
              class="link"
              type="button"
              :disabled="busy"
              @click="dispatch(batch)"
            >
              派发
            </button>
            <button class="link" type="button" :disabled="busy" @click="openReceipt(batch, '成功')">成功回执</button>
            <button class="link" type="button" :disabled="busy" @click="openReceipt(batch, '中断')">中断回执</button>
          </td>
        </tr>
        <tr v-if="!batches.length">
          <td colspan="9" class="empty-state">暂无补采批次，先提交补采申请</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">补采申请与实际覆盖</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>申请编号</th><th>申请时段</th><th>状态</th><th>已覆盖</th><th>未覆盖</th>
          <th>实际补采时段</th><th>回执</th><th>分时批次</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="app in applications" :key="app.id" :data-testid="`collection-app-${app.申请编号}`">
          <td>{{ app.申请编号 }}</td>
          <td>{{ app.时段 }}</td>
          <td>{{ app.状态 }}</td>
          <td>{{ app.已覆盖时段 }}</td>
          <td>{{ app.未覆盖时段 }}</td>
          <td>
            <div v-for="item in app.实际补采时段" :key="`${item.回执编号}-${item.时段}`">
              {{ item.时段 }} {{ item.结果 }}{{ item.入账 ? `（入账${item.入账分钟}分）` : '（未入账）' }}
            </div>
          </td>
          <td>
            <div v-for="item in app.回执" :key="item.回执编号">
              {{ item.回执编号 }} {{ item.结果 }}
            </div>
          </td>
          <td>
            <div v-for="item in app.分时批次" :key="item.批次编号">
              {{ item.时段 }} {{ item.状态 }} → {{ item.请求批次编号 }}
            </div>
          </td>
        </tr>
        <tr v-if="!applications.length">
          <td colspan="8" class="empty-state">暂无补采申请</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">已入账覆盖</h3>
    <table class="data-table">
      <thead>
        <tr><th>覆盖区间</th><th>关联端点</th><th>来源批次</th><th>来源回执</th></tr>
      </thead>
      <tbody>
        <tr v-for="item in coverage" :key="`${item.来源回执}-${item.时段}`">
          <td>{{ item.时段 }}</td>
          <td>{{ item.关联端点 }}</td>
          <td>{{ item.来源批次 }}</td>
          <td>{{ item.来源回执 }}</td>
        </tr>
        <tr v-if="!coverage.length">
          <td colspan="4" class="empty-state">暂无成功入账的覆盖区间</td>
        </tr>
      </tbody>
    </table>

    <div v-if="receipt" class="modal-backdrop" data-testid="collection-receipt-modal">
      <form class="modal" @submit.prevent="submitReceipt">
        <h3>{{ receipt.batch.批次编号 }} · {{ receipt.batch.时段 }}（{{ receipt.result }}）</h3>
        <label class="create-field">
          <span>本次实际处理开始</span>
          <input v-model="receipt.start" data-testid="receipt-start" placeholder="10:00" />
        </label>
        <label class="create-field">
          <span>本次实际处理结束</span>
          <input v-model="receipt.end" data-testid="receipt-end" placeholder="10:30" />
        </label>
        <label class="create-field">
          <span>返回行数</span>
          <input v-model="receipt.rows" data-testid="receipt-rows" />
        </label>
        <label class="create-field">
          <span>备注</span>
          <input v-model="receipt.remark" data-testid="receipt-remark" />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="receipt = null">取消</button>
          <button class="btn primary" data-testid="receipt-submit" type="submit" :disabled="busy">
            确认登记回执
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ applications.length }} 条申请 / {{ batches.length }} 个分时批次</span>
      <span v-if="summary">待重放片段 {{ summary.待重放片段 }}，已入账 {{ summary.已入账覆盖分钟 }} 分钟</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import {
  dispatchCollectionBatch,
  listCollectionApplications,
  listCollectionBatches,
  loadCollectionSummary,
  registerCollectionReceipt,
  resumeCollection,
  submitCollectionApplications,
} from '@/api/backend-service'
import type {
  CollectionApplication,
  CollectionBatch,
  CollectionSummary,
} from '@/api/backend-service'

const endpoint = ref('')
const applications = ref<CollectionApplication[]>([])
const batches = ref<CollectionBatch[]>([])
const summary = ref<CollectionSummary | null>(null)
const coverage = computed(() => summary.value?.覆盖明细 ?? [])
const message = ref('')
const errorMessage = ref('')
const busy = ref(false)
const form = reactive({ endpoint: 'ENDP-CSV-0001', date: '', windows: '08:00-10:00\n09:00-11:00' })
const receipt = ref<{
  batch: CollectionBatch
  result: string
  start: string
  end: string
  rows: string
  remark: string
} | null>(null)

const stats = computed(() => [
  { label: '补采申请', value: summary.value?.申请总数 ?? 0 },
  { label: '分时批次', value: summary.value?.批次总数 ?? 0 },
  { label: '成功回执', value: summary.value?.成功回执 ?? 0 },
  { label: '已入账分钟', value: summary.value?.已入账覆盖分钟 ?? 0 },
  { label: '待重放片段', value: summary.value?.待重放片段 ?? 0 },
])

const TIME_RANGE = /(\d{1,2}:\d{2}(?::\d{2})?)\s*(?:~|～|至|到|-|—)\s*(\d{1,2}:\d{2}(?::\d{2})?)/

function parseWindows(text: string): { 开始: string; 结束: string }[] {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const match = line.match(TIME_RANGE)
      if (!match) throw new Error(`无法识别补采时段：${line}`)
      return { 开始: match[1], 结束: match[2] }
    })
}

function canDispatch(batch: CollectionBatch): boolean {
  return ['待派发', '待重放', '已中断', '部分成功'].includes(batch.状态)
}

function openWindow(未覆盖时段: string): { start: string; end: string } {
  const match = 未覆盖时段.match(/^(\d{1,2}:\d{2}(?::\d{2})?)-(\d{1,2}:\d{2}(?::\d{2})?)$/)
  return match ? { start: match[1], end: match[2] } : { start: '', end: '' }
}

async function reload() {
  try {
    endpoint.value = form.endpoint.trim()
    const [apps, batchRows, totals] = await Promise.all([
      listCollectionApplications(endpoint.value),
      listCollectionBatches(endpoint.value),
      loadCollectionSummary(endpoint.value),
    ])
    applications.value = apps
    batches.value = batchRows
    summary.value = totals
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集处理数据读取失败'
  }
}

async function submit() {
  busy.value = true
  errorMessage.value = ''
  message.value = ''
  try {
    const result = await submitCollectionApplications({
      端点: form.endpoint.trim(),
      日期: form.date.trim() || undefined,
      申请: parseWindows(form.windows),
    })
    message.value = result.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '补采申请提交失败'
  } finally {
    busy.value = false
  }
}

async function dispatch(batch: CollectionBatch) {
  busy.value = true
  errorMessage.value = ''
  message.value = ''
  try {
    const result = await dispatchCollectionBatch(batch.id)
    message.value = result.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '派发失败'
  } finally {
    busy.value = false
  }
}

function openReceipt(batch: CollectionBatch, result: string) {
  const window = openWindow(batch.未覆盖时段)
  receipt.value = {
    batch,
    result,
    start: window.start,
    end: window.end,
    rows: '',
    remark: '',
  }
}

async function submitReceipt() {
  if (!receipt.value) return
  busy.value = true
  errorMessage.value = ''
  message.value = ''
  try {
    const result = await registerCollectionReceipt(receipt.value.batch.id, {
      结果: receipt.value.result,
      开始: receipt.value.start.trim() || undefined,
      结束: receipt.value.end.trim() || undefined,
      返回行数: receipt.value.rows ? Number(receipt.value.rows) : undefined,
      备注: receipt.value.remark.trim() || undefined,
    })
    message.value = result.message
    receipt.value = null
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '回执登记失败'
  } finally {
    busy.value = false
  }
}

async function resume() {
  busy.value = true
  errorMessage.value = ''
  message.value = ''
  try {
    const result = await resumeCollection({ 端点: endpoint.value || undefined })
    message.value = result.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '恢复处理失败'
  } finally {
    busy.value = false
  }
}

onMounted(reload)
</script>
