/**
 * 卡密库存管理弹窗组件（方案 docs/card-secret-stock-plan.md §4 前端）
 *
 * 功能：
 * 1. 库存总览：可用/已用/作废数量，库存为空时红色警示
 * 2. 补货：文本卡密多行导入（按行拆分）+ 二维码图片多图批量上传（按 MD5 去重）
 * 3. 使用记录：分页查看已发卡密及对应订单，图片卡密显示缩略图（点击放大）
 */
import { useState, useEffect, useRef, type ChangeEvent } from 'react'
import {
  X, Loader2, RefreshCw, Package, Upload, ImagePlus,
  ChevronLeft, ChevronRight, Layers,
} from 'lucide-react'
import {
  getCardStock, getCardUsageRecords, addCardSecrets, uploadCardSecretImages,
  type CardData, type CardStockInfo, type CardSecretUsageRecord,
} from '@/api/cards'
import { useUIStore } from '@/store/uiStore'

// 使用记录每页条数
const RECORD_PAGE_SIZE = 10
// 图片卡密单批上传上限（与后端一致）
const IMAGE_BATCH_LIMIT = 50
// 单张图片大小上限（与后端一致）
const IMAGE_MAX_SIZE = 5 * 1024 * 1024

// 判断使用记录内容是否为图片卡密（content 存图片相对URL）
const isImageContent = (content: string) => content.startsWith('/static/uploads/')

interface CardSecretStockModalProps {
  /** 卡密分类（data 型卡券） */
  card: CardData
  /** 关闭回调 */
  onClose: () => void
  /** 库存变动回调（补货成功后刷新外层列表） */
  onChanged: () => void
}

export function CardSecretStockModal({ card, onClose, onChanged }: CardSecretStockModalProps) {
  const { addToast } = useUIStore()
  const cardId = card.id!

  // 库存总览
  const [stock, setStock] = useState<CardStockInfo | null>(null)
  const [stockLoading, setStockLoading] = useState(true)

  // 页签：restock-补货 / records-使用记录
  const [activeTab, setActiveTab] = useState<'restock' | 'records'>('restock')

  // 文本补货
  const [textContent, setTextContent] = useState('')
  const [textSaving, setTextSaving] = useState(false)

  // 图片补货
  const [imageFiles, setImageFiles] = useState<File[]>([])
  const [imageSaving, setImageSaving] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

  // 使用记录
  const [records, setRecords] = useState<CardSecretUsageRecord[]>([])
  const [recordTotal, setRecordTotal] = useState(0)
  const [recordPage, setRecordPage] = useState(1)
  const [recordsLoading, setRecordsLoading] = useState(true)

  // 图片预览
  const [previewUrl, setPreviewUrl] = useState('')

  // 加载库存总览
  const loadStock = async () => {
    setStockLoading(true)
    try {
      const result = await getCardStock(cardId)
      if (result.success && result.data) {
        setStock(result.data)
      } else {
        addToast({ type: 'error', message: result.message || '加载库存失败' })
      }
    } catch {
      addToast({ type: 'error', message: '加载库存失败' })
    } finally {
      setStockLoading(false)
    }
  }

  // 加载使用记录（分页）
  const loadRecords = async (page: number) => {
    setRecordsLoading(true)
    try {
      const result = await getCardUsageRecords(cardId, page, RECORD_PAGE_SIZE)
      if (result.success && result.data) {
        setRecords(result.data.items || [])
        setRecordTotal(result.data.total || 0)
        setRecordPage(page)
      } else {
        addToast({ type: 'error', message: result.message || '加载使用记录失败' })
      }
    } catch {
      addToast({ type: 'error', message: '加载使用记录失败' })
    } finally {
      setRecordsLoading(false)
    }
  }

  useEffect(() => {
    loadStock()
    loadRecords(1)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 补货成功后的统一刷新：库存 + 使用记录 + 外层列表
  const refreshAfterRestock = () => {
    loadStock()
    loadRecords(1)
    onChanged()
  }

  // 文本卡密补货
  const handleTextRestock = async () => {
    if (!textContent.trim()) {
      addToast({ type: 'warning', message: '请先输入卡密内容（每行一条）' })
      return
    }
    setTextSaving(true)
    try {
      const result = await addCardSecrets(cardId, textContent)
      if (result.success) {
        addToast({ type: 'success', message: `成功导入 ${result.data?.added ?? 0} 条卡密` })
        setTextContent('')
        refreshAfterRestock()
      } else {
        addToast({ type: 'error', message: result.message || '导入失败' })
      }
    } catch {
      addToast({ type: 'error', message: '导入失败' })
    } finally {
      setTextSaving(false)
    }
  }

  // 图片选择（追加，含数量/大小/类型前置校验）
  const handleImageSelect = (e: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || [])
    e.target.value = ''
    if (files.length === 0) return

    const invalid = files.find(f => !f.type.startsWith('image/'))
    if (invalid) {
      addToast({ type: 'error', message: `文件 ${invalid.name} 不是图片` })
      return
    }
    const oversized = files.find(f => f.size > IMAGE_MAX_SIZE)
    if (oversized) {
      addToast({ type: 'error', message: `图片 ${oversized.name} 超过 5MB` })
      return
    }
    setImageFiles(prev => {
      const merged = [...prev, ...files]
      if (merged.length > IMAGE_BATCH_LIMIT) {
        addToast({ type: 'warning', message: `单批最多上传 ${IMAGE_BATCH_LIMIT} 张图片` })
        return merged.slice(0, IMAGE_BATCH_LIMIT)
      }
      return merged
    })
  }

  // 图片卡密补货
  const handleImageRestock = async () => {
    if (imageFiles.length === 0) {
      addToast({ type: 'warning', message: '请先选择二维码图片' })
      return
    }
    setImageSaving(true)
    try {
      const result = await uploadCardSecretImages(cardId, imageFiles)
      if (result.success) {
        const added = result.data?.added ?? 0
        const skipped = result.data?.skipped ?? 0
        addToast({
          type: 'success',
          message: skipped > 0
            ? `成功导入 ${added} 张，${skipped} 张重复已跳过`
            : `成功导入 ${added} 张卡密图片`,
        })
        setImageFiles([])
        refreshAfterRestock()
      } else {
        addToast({ type: 'error', message: result.message || '导入失败' })
      }
    } catch {
      addToast({ type: 'error', message: '导入失败' })
    } finally {
      setImageSaving(false)
    }
  }

  const recordTotalPages = Math.max(1, Math.ceil(recordTotal / RECORD_PAGE_SIZE))

  return (
    <div className="modal-overlay" style={{ zIndex: 60 }}>
      <div className="modal-content max-w-3xl max-h-[90vh] overflow-hidden flex flex-col">
        <div className="modal-header flex items-center justify-between flex-shrink-0">
          <div>
            <h2 className="text-lg font-semibold flex items-center gap-2">
              <Layers className="w-5 h-5 text-blue-500" />
              卡密库存管理
            </h2>
            <p className="text-sm text-gray-500 mt-1">分类: {card.name}</p>
          </div>
          <button onClick={onClose} className="p-1 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-lg">
            <X className="w-4 h-4 text-gray-500" />
          </button>
        </div>

        <div className="modal-body flex-1 overflow-y-auto space-y-4">
          {/* 库存总览 */}
          <div className="grid grid-cols-3 gap-3">
            <div className={`rounded-lg p-3 text-center border ${
              stock && stock.available === 0
                ? 'bg-red-50 border-red-200 dark:bg-red-900/20 dark:border-red-800'
                : 'bg-green-50 border-green-200 dark:bg-green-900/20 dark:border-green-800'
            }`}>
              <p className={`text-2xl font-bold ${
                stock && stock.available === 0
                  ? 'text-red-600 dark:text-red-400'
                  : 'text-green-600 dark:text-green-400'
              }`}>
                {stockLoading ? '-' : stock?.available ?? 0}
              </p>
              <p className="text-xs text-gray-500 mt-1">
                可用库存{stock && stock.available === 0 && '（已空，商品将下架）'}
              </p>
            </div>
            <div className="rounded-lg p-3 text-center border bg-blue-50 border-blue-200 dark:bg-blue-900/20 dark:border-blue-800">
              <p className="text-2xl font-bold text-blue-600 dark:text-blue-400">
                {stockLoading ? '-' : stock?.used ?? 0}
              </p>
              <p className="text-xs text-gray-500 mt-1">已使用</p>
            </div>
            <div className="rounded-lg p-3 text-center border bg-gray-50 border-gray-200 dark:bg-gray-800 dark:border-gray-700">
              <p className="text-2xl font-bold text-gray-500 dark:text-gray-400">
                {stockLoading ? '-' : stock?.void ?? 0}
              </p>
              <p className="text-xs text-gray-500 mt-1">已作废</p>
            </div>
          </div>

          {/* 页签 */}
          <div className="flex border-b border-gray-200 dark:border-gray-700">
            {(['restock', 'records'] as const).map(tab => (
              <button
                key={tab}
                onClick={() => setActiveTab(tab)}
                className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors ${
                  activeTab === tab
                    ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                    : 'border-transparent text-gray-500 hover:text-gray-700 dark:hover:text-gray-300'
                }`}
              >
                {tab === 'restock' ? '补货' : `使用记录 (${recordTotal})`}
              </button>
            ))}
            <button
              onClick={() => { loadStock(); loadRecords(recordPage) }}
              className="ml-auto p-2 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
              title="刷新"
            >
              <RefreshCw className="w-4 h-4" />
            </button>
          </div>

          {/* 补货页签 */}
          {activeTab === 'restock' && (
            <div className="space-y-5">
              {/* 文本卡密补货 */}
              <div>
                <h3 className="text-sm font-medium text-gray-900 dark:text-white mb-2">文本卡密（每行一条）</h3>
                <textarea
                  value={textContent}
                  onChange={e => setTextContent(e.target.value)}
                  placeholder={'每行输入一条卡密，例如：\nABC-12345\nDEF-67890'}
                  rows={5}
                  className="input-ios w-full font-mono text-xs"
                />
                <div className="flex items-center justify-between mt-2">
                  <p className="text-xs text-gray-500">与存量重复及空行会自动跳过</p>
                  <button
                    onClick={handleTextRestock}
                    disabled={textSaving || !textContent.trim()}
                    className="btn-ios-primary"
                  >
                    {textSaving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Upload className="w-4 h-4" />}
                    导入文本卡密
                  </button>
                </div>
              </div>

              {/* 图片卡密补货 */}
              <div className="border-t border-gray-200 dark:border-gray-700 pt-4">
                <h3 className="text-sm font-medium text-gray-900 dark:text-white mb-2">二维码图片卡密（售出后发送图片）</h3>
                <div className="flex flex-wrap gap-2">
                  {imageFiles.map((file, i) => (
                    <div key={`${file.name}-${i}`} className="relative group">
                      <img
                        src={URL.createObjectURL(file)}
                        alt={file.name}
                        className="w-16 h-16 object-cover rounded-lg border border-gray-200 dark:border-gray-700"
                      />
                      <button
                        onClick={() => setImageFiles(prev => prev.filter((_, idx) => idx !== i))}
                        className="absolute -top-1.5 -right-1.5 w-4 h-4 bg-red-500 text-white rounded-full text-[10px] leading-none hidden group-hover:flex items-center justify-center"
                        title="移除"
                      >
                        ×
                      </button>
                    </div>
                  ))}
                  {imageFiles.length < IMAGE_BATCH_LIMIT && (
                    <label className="w-16 h-16 flex flex-col items-center justify-center border-2 border-dashed border-gray-300 dark:border-gray-600 rounded-lg cursor-pointer hover:border-blue-400 transition-colors">
                      <input
                        ref={fileInputRef}
                        type="file"
                        accept="image/*"
                        multiple
                        className="hidden"
                        onChange={handleImageSelect}
                      />
                      <ImagePlus className="w-5 h-5 text-gray-400" />
                      <span className="text-[10px] text-gray-400 mt-0.5">添加</span>
                    </label>
                  )}
                </div>
                <div className="flex items-center justify-between mt-2">
                  <p className="text-xs text-gray-500">
                    已选 {imageFiles.length} 张，单批最多 {IMAGE_BATCH_LIMIT} 张，单张 ≤5MB；重复图片按字节 MD5 自动跳过
                  </p>
                  <button
                    onClick={handleImageRestock}
                    disabled={imageSaving || imageFiles.length === 0}
                    className="btn-ios-primary"
                  >
                    {imageSaving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Upload className="w-4 h-4" />}
                    导入图片卡密
                  </button>
                </div>
              </div>
            </div>
          )}

          {/* 使用记录页签 */}
          {activeTab === 'records' && (
            <div>
              {recordsLoading ? (
                <div className="flex items-center justify-center py-10 text-gray-400">
                  <Loader2 className="w-5 h-5 animate-spin mr-2" />
                  <span className="text-sm">加载中...</span>
                </div>
              ) : records.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-10 text-gray-400">
                  <Package className="w-8 h-8 mb-2" />
                  <p className="text-sm">暂无使用记录</p>
                </div>
              ) : (
                <>
                  <table className="table-ios">
                    <thead>
                      <tr>
                        <th className="whitespace-nowrap">卡密内容</th>
                        <th className="whitespace-nowrap">订单号</th>
                        <th className="whitespace-nowrap">使用时间</th>
                      </tr>
                    </thead>
                    <tbody>
                      {records.map((record, i) => (
                        <tr key={`${record.order_id}-${i}`}>
                          <td className="max-w-[280px]">
                            {isImageContent(record.content) ? (
                              <button onClick={() => setPreviewUrl(record.content)} title="点击放大">
                                <img
                                  src={record.content}
                                  alt="二维码卡密"
                                  className="w-12 h-12 object-cover rounded border border-gray-200 dark:border-gray-700"
                                />
                              </button>
                            ) : (
                              <code className="text-xs bg-gray-100 dark:bg-gray-800 px-2 py-1 rounded break-all">
                                {record.content}
                              </code>
                            )}
                          </td>
                          <td className="text-xs text-gray-600 dark:text-gray-400 whitespace-nowrap">
                            {record.order_id}
                          </td>
                          <td className="text-xs text-gray-500 whitespace-nowrap">
                            {record.used_at ? new Date(record.used_at).toLocaleString('zh-CN') : '-'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>

                  {/* 分页 */}
                  {recordTotal > RECORD_PAGE_SIZE && (
                    <div className="flex items-center justify-end gap-2 mt-3">
                      <span className="text-xs text-gray-500">
                        第 {recordPage} / {recordTotalPages} 页，共 {recordTotal} 条
                      </span>
                      <button
                        onClick={() => loadRecords(recordPage - 1)}
                        disabled={recordPage <= 1}
                        className="p-1.5 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50"
                      >
                        <ChevronLeft className="w-4 h-4" />
                      </button>
                      <button
                        onClick={() => loadRecords(recordPage + 1)}
                        disabled={recordPage >= recordTotalPages}
                        className="p-1.5 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50"
                      >
                        <ChevronRight className="w-4 h-4" />
                      </button>
                    </div>
                  )}
                </>
              )}
            </div>
          )}
        </div>

        <div className="modal-footer flex-shrink-0">
          <button onClick={onClose} className="btn-ios-secondary">关闭</button>
        </div>
      </div>

      {/* 图片预览（放大二维码） */}
      {previewUrl && (
        <div className="modal-overlay" style={{ zIndex: 70 }} onClick={() => setPreviewUrl('')}>
          <div className="modal-content max-w-md">
            <div className="modal-body flex items-center justify-center">
              <img src={previewUrl} alt="二维码卡密" className="max-w-full max-h-[60vh] object-contain rounded" />
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
