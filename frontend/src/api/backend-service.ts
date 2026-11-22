import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ---------- 采集处理（补采申请/分时批次/回执/覆盖） ----------
export type CollectionApplication = {
  id: number
  申请编号: string
  关联端点: string
  时段: string
  开始时间: string
  结束时间: string
  状态: string
  已覆盖时段: string
  未覆盖时段: string
  覆盖分钟数: number
  实际补采时段: {
    回执编号: string
    批次编号: string
    时段: string
    结果: string
    入账: boolean
    入账分钟: number
  }[]
  回执: { 回执编号: string; 批次编号: string; 时段: string; 结果: string; 入账分钟: number }[]
  分时批次: {
    批次编号: string
    时段: string
    状态: string
    结果: string
    补采结论: string
    已覆盖时段: string
    未覆盖时段: string
    请求批次编号: string
  }[]
  补采清单: Record<string, string | number | string[]>[]
}

export type CollectionBatch = {
  id: number
  批次编号: string
  关联端点: string
  时段: string
  开始时间: string
  结束时间: string
  覆盖申请: string[]
  状态: string
  在途: boolean
  结果: string
  补采结论: string
  已覆盖时段: string
  未覆盖时段: string
  入账分钟: number
  请求批次编号: string
  回执明细: { 回执编号: string; 时段: string; 结果: string; 入账分钟: number }[]
}

export type CollectionSummary = {
  申请总数: number
  批次总数: number
  回执总数: number
  成功回执: number
  覆盖区间数: number
  已入账覆盖分钟: number
  待重放片段: number
  申请状态: Record<string, number>
  批次状态: Record<string, number>
  覆盖明细: { 时段: string; 关联端点: string; 来源批次: string; 来源回执: string }[]
}

export type SubmitSegment = {
  批次编号: string
  时段: string
  覆盖申请: string[]
  状态: string
  补采结论: string
  请求批次编号: string
  新建: boolean
}

export type SubmitResult = {
  ok: boolean
  message: string
  关联端点: string
  reconciled_receipts: number
  reused_applications: string[]
  applications: CollectionApplication[]
  batches: CollectionBatch[]
  segments: SubmitSegment[]
}

export type ResumeResult = {
  ok: boolean
  message: string
  核对回执: number
  补记覆盖: number
  应执行片段: { id: number; 批次编号: string; 时段: string; 状态: string; 请求批次编号: string }[]
  跳过片段: { id: number; 批次编号: string; 时段: string; 原因: string }[]
}

export async function loadCollectionSummary(endpoint = ''): Promise<CollectionSummary> {
  const query = endpoint ? `?endpoint=${encodeURIComponent(endpoint)}` : ''
  const response = await request(`/api/collection/summary${query}`)
  if (!response.ok) throw new Error('补采概览读取失败')
  return await response.json()
}

export async function listCollectionApplications(endpoint = ''): Promise<CollectionApplication[]> {
  const query = endpoint ? `?endpoint=${encodeURIComponent(endpoint)}` : ''
  const response = await request(`/api/collection/applications${query}`)
  if (!response.ok) throw new Error('补采申请读取失败')
  return (await response.json()).items
}

export async function listCollectionBatches(endpoint = ''): Promise<CollectionBatch[]> {
  const query = endpoint ? `?endpoint=${encodeURIComponent(endpoint)}` : ''
  const response = await request(`/api/collection/batches${query}`)
  if (!response.ok) throw new Error('补采批次读取失败')
  return (await response.json()).items
}

export async function submitCollectionApplications(body: {
  端点: string
  日期?: string
  申请: { 开始: string; 结束: string }[]
}): Promise<SubmitResult> {
  const response = await request('/api/collection/submit', {
    method: 'POST',
    body: JSON.stringify(body),
  })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.detail ?? '补采申请提交失败')
  return payload
}

export async function dispatchCollectionBatch(id: number): Promise<ActionResult> {
  const response = await request(`/api/collection/batches/${id}/dispatch`, { method: 'POST' })
  return await response.json()
}

export async function registerCollectionReceipt(
  id: number,
  body: {
    结果: string
    开始?: string
    结束?: string
    返回行数?: number
    备注?: string
  },
): Promise<ActionResult> {
  const response = await request(`/api/collection/batches/${id}/receipt`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.detail ?? '回执登记失败')
  return payload
}

export async function resumeCollection(body: { 端点?: string }): Promise<ResumeResult> {
  const response = await request('/api/collection/resume', {
    method: 'POST',
    body: JSON.stringify(body),
  })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.detail ?? '恢复处理失败')
  return payload
}
