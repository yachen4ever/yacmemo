<template>
  <div class="topics-page">
    <n-split direction="horizontal" :max="0.8" :min="0.15" :default-size="0.25">
      <template #1>
        <div class="topic-tree">
          <div class="topic-toolbar">
            <n-select v-model:value="tagFilter" multiple clearable size="small"
              :options="tagOptions" :placeholder="t('按标签过滤')" :max-tag-count="2" />
            <n-button size="small" @click="tagModal = true">{{ t('标签管理') }}</n-button>
          </div>
          <n-spin v-if="loading" size="small" />
          <n-tree
            v-else
            :data="treeData"
            :selectable="true"
            @update:selected-keys="onSelect"
            block-line
            expand-on-click
            :default-expanded-keys="expandedKeys"
          />
        </div>
      </template>
      <template #2>
        <div class="topic-detail">
          <n-empty v-if="!selectedNote" :description="t('选择左侧主题或笔记查看内容')" />
          <template v-else>
            <n-space justify="space-between" align="center" style="margin-bottom: 12px">
              <n-text strong style="font-size: 15px">{{ selectedNoteTitle }}</n-text>
              <n-space>
                <n-button size="small" @click="toggleEdit">
                  {{ editing ? t('预览') : t('编辑') }}
                </n-button>
                <n-button v-if="canArchiveSelected" size="small"
                  @click="archiveSelected">{{ t('归档') }}</n-button>
                <n-button v-if="canUnarchiveSelected" size="small"
                  @click="unarchiveSelected">{{ t('取消归档') }}</n-button>
                <n-button size="small" type="error" ghost @click="handleDelete"
                  v-if="!isAbstract">{{ t('删除') }}</n-button>
              </n-space>
            </n-space>
            <n-input
              v-if="editing"
              v-model:value="editContent"
              type="textarea"
              :rows="20"
              style="font-family: monospace"
            />
            <div v-else class="markdown-body" v-html="renderedContent" />
          </template>
        </div>
      </template>
    </n-split>

    <!-- 标签管理：标签清单（重命名/删除）+ 按主题打标签 -->
    <n-modal v-model:show="tagModal" preset="card" :title="t('标签管理')" style="width: 580px">
      <n-space vertical size="large">
        <div>
          <n-text strong>{{ t('标签清单') }}</n-text>
          <n-empty v-if="!Object.keys(tagCounts).length" :description="t('暂无标签')"
            size="small" style="margin-top: 6px" />
          <n-list v-else style="margin-top: 6px">
            <n-list-item v-for="(cnt, tag) in tagCounts" :key="tag">
              <template v-if="renaming === tag">
                <n-space :size="4" :wrap="false">
                  <n-input v-model:value="renameInput" size="small" style="width: 140px" />
                  <n-button size="tiny" type="primary" @click="doRename(tag)">{{ t('保存') }}</n-button>
                  <n-button size="tiny" @click="renaming = ''">{{ t('取消') }}</n-button>
                </n-space>
              </template>
              <n-space v-else justify="space-between" align="center">
                <n-space align="center" :size="6">
                  <n-tag size="small">{{ tag }}</n-tag>
                  <n-text depth="3" style="font-size: 12px">{{ cnt }} {{ t('个主题') }}</n-text>
                </n-space>
                <n-space :size="4">
                  <n-button size="tiny" @click="startRename(tag)">{{ t('重命名') }}</n-button>
                  <n-popconfirm @positive-click="doDeleteTag(tag)">
                    <template #trigger>
                      <n-button size="tiny" type="error" ghost>{{ t('删除') }}</n-button>
                    </template>
                    {{ t('从所有主题移除该标签？主题本身不受影响。') }}
                  </n-popconfirm>
                </n-space>
              </n-space>
            </n-list-item>
          </n-list>
        </div>
        <n-divider style="margin: 0" />
        <div>
          <n-text strong>{{ t('按主题打标签') }}</n-text>
          <n-space vertical size="small" style="margin-top: 6px">
            <n-select v-model:value="tagTopic" filterable
              :options="topicOptions" :placeholder="t('选择主题')" />
            <template v-if="tagTopic">
              <n-space align="center" :size="4">
                <n-tag v-for="tg in tagsOf(tagTopic)" :key="tg" size="small" closable
                  @close="removeTag(tagTopic, tg)">{{ tg }}</n-tag>
                <n-text v-if="!tagsOf(tagTopic).length" depth="3" style="font-size: 12px">
                  {{ t('暂无标签') }}
                </n-text>
              </n-space>
              <n-select v-model:value="newTag" filterable tag clearable
                :options="tagOptions" :placeholder="t('添加标签（可选已有或输入新标签）')"
                @update:value="addTag" />
            </template>
          </n-space>
        </div>
      </n-space>
    </n-modal>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import { NSplit, NSpin, NTree, NEmpty, NText, NSpace, NButton, NInput, NSelect,
         NModal, NList, NListItem, NTag, NDivider, NPopconfirm, useMessage, useDialog } from 'naive-ui'
import { marked } from 'marked'
import { api, params } from '../composables/api.js'
import { t } from '../composables/i18n.js'

const props = defineProps({ user: String })
const message = useMessage()
const dialog = useDialog()

const loading = ref(true)
const topics = ref([])
const notes = ref([])
const selectedNote = ref(null)
const selectedNoteTitle = ref('')
const editing = ref(false)
const editContent = ref('')
const isAbstract = ref(false)

const expandedKeys = ref([])
const tagFilter = ref([])
const tagModal = ref(false)
const renaming = ref('')
const renameInput = ref('')
const tagTopic = ref('')
const newTag = ref(null)

const tagsOfTitle = (title) => {
  const t = topics.value.find(x => x.title === title)
  return t?.tags || []
}
const tagsOf = tagsOfTitle

const tagOptions = computed(() =>
  Object.keys(tagCounts.value).map(x => ({ label: x, value: x })))

const tagCounts = computed(() => {
  const c = {}
  for (const t of topics.value)
    for (const x of t.tags || []) c[x] = (c[x] || 0) + 1
  return c
})

const topicOptions = computed(() =>
  topics.value.map(x => ({ label: x.title + (x.archived ? `（${t('已归档')}）` : ''), value: x.title })))

const treeData = computed(() => {
  const result = []
  const noteNode = n => ({
    key: `note:${n.path}`,
    label: n.title || n.path.split('/').pop().replace('.md', ''),
    isLeaf: true,
  })
  const dirOf = p => p.split('/').slice(0, -1).join('/')
  // 与后端 _path_covered 同款覆盖口径：注册主题的卡/相关文件及其所在目录
  const coveredFiles = new Set()
  const coveredDirs = new Set()
  for (const t of topics.value) {
    for (const p of [t.card, ...(t.related || [])]) {
      if (!p) continue
      coveredFiles.add(p)
      const d = dirOf(p)
      if (d) coveredDirs.add(d)
    }
  }
  const isCovered = path => coveredFiles.has(path) ||
    [...coveredDirs].some(d => path.startsWith(d + '/'))
  const tagSel = tagFilter.value || []
  const tagMatch = t => !tagSel.length || (t.tags || []).some(x => tagSel.includes(x))
  const topicLabel = t => t.title +
    ((t.tags || []).length ? ` 〔${t.tags.join('·')}〕` : '')
  // Active topics：每主题一目录，目录即归属（按卡所在目录取模块笔记）
  const activeChildren = []
  for (const t of topics.value.filter(t => !t.archived && tagMatch(t))) {
    const dir = dirOf(t.card)
    const topicNotes = notes.value.filter(n =>
      dir && n.path.startsWith(dir + '/') && n.path !== t.card)
    activeChildren.push({
      key: `topic:${t.title}`,
      label: topicLabel(t),
      children: [
        { key: `note:${t.card}`, label: 'abstract', isLeaf: true },
        ...topicNotes.map(noteNode),
      ],
    })
  }
  result.push({ key: 'active', label: `${t('活跃主题')} (${activeChildren.length})`, children: activeChildren })
  // Archived topics：卡已被后端改写到 archive/<主题>/，同样展开目录下全部文件
  const archived = topics.value.filter(t => t.archived && tagMatch(t))
  if (archived.length) {
    result.push({
      key: 'archived',
      label: `${t('已归档')} (${archived.length})`,
      children: archived.map(t => {
        const dir = dirOf(t.card)
        const files = notes.value.filter(n =>
          dir && n.path.startsWith(dir + '/') && n.path !== t.card)
        return {
          key: `topic:${t.title}`,
          label: topicLabel(t),
          children: [
            { key: `note:${t.card}`, label: 'abstract', isLeaf: true },
            ...files.map(noteNode),
          ],
        }
      }),
    })
  }
  // 散归档文件：archive/ 下不属于任何注册归档主题目录的 md
  //（单篇归档产物；注册归档主题目录内的文件已在上面挂出）
  const archivedDirs = archived.map(t => dirOf(t.card)).filter(Boolean)
  const strayArchived = notes.value.filter(n =>
    n.path.startsWith('archive/') &&
    !archivedDirs.some(d => n.path.startsWith(d + '/')))
  if (strayArchived.length) {
    result.push({
      key: 'stray-archived',
      label: `${t('单篇归档')} (${strayArchived.length})`,
      children: strayArchived.map(noteNode),
    })
  }
  // Free zones
  result.push({ key: 'free', label: t('免注册区'), children: [
    { key: 'zone:journal', label: 'journal', children: notes.value
      .filter(n => n.path.startsWith('journal/'))
      .map(noteNode) },
    { key: 'zone:curator', label: 'curator', children: notes.value
      .filter(n => n.path.startsWith('curator/'))
      .map(noteNode) },
  ]})
  // 专属记忆（agents/）：shared/ = agent 层共享子树，<device>/ = 本机专属
  //（与后端 identity 分层同构）
  const agentNotes = notes.value.filter(n => n.path.startsWith('agents/'))
  if (agentNotes.length) {
    const byAgent = {}
    for (const n of agentNotes) {
      const seg = n.path.split('/')
      const agent = seg[1] || t('（未分组）')
      const g = (byAgent[agent] = byAgent[agent] || { shared: [], devices: {} })
      if (seg[2] === 'shared') g.shared.push(n)
      else if (seg.length >= 4) {
        (g.devices[seg[2]] = g.devices[seg[2]] || []).push(n)
      }
    }
    result.push({
      key: 'agents',
      label: `${t('专属记忆')} (${agentNotes.length})`,
      children: Object.entries(byAgent).map(([agent, g]) => ({
        key: `agent:${agent}`,
        label: agent,
        children: [
          ...g.shared.map(noteNode),
          ...Object.entries(g.devices).map(([dev, ns]) => ({
            key: `agent:${agent}:${dev}`,
            label: `${dev} (${ns.length})`,
            children: ns.map(noteNode),
          })),
        ],
      })),
    })
  }
  // 游离文件：与后端 D4 同口径（免注册区 + 系统文件 + 专属区 + 注册覆盖之外）
  const strays = notes.value.filter(n =>
    !n.path.startsWith('journal/') && !n.path.startsWith('archive/') &&
    !n.path.startsWith('curator/') && !n.path.startsWith('agents/') &&
    n.path !== 'TOPICS.md' && n.path !== 'PROFILE.md' &&
    !isCovered(n.path))
  result.push({ key: 'stray', label: `${t('游离文件')} (${strays.length})`,
    children: strays.map(noteNode) })
  const system = notes.value.filter(n => n.path === 'TOPICS.md' || n.path === 'PROFILE.md')
  if (system.length) {
    result.push({ key: 'system', label: t('系统文件'), children: system.map(noteNode) })
  }
  return result
})

const renderedContent = computed(() => {
  if (!selectedNote.value) return ''
  return marked.parse(selectedNote.value.content || '')
})

async function loadData() {
  if (!props.user) return
  loading.value = true
  try {
    const [topicData, noteData] = await Promise.all([
      api(`/api/${props.user}/topics`),
      api(`/api/${props.user}/notes${params({ sort: 'name' })}`),
    ])
    topics.value = [...topicData.active, ...topicData.archived.map(a => ({ ...a, archived: true }))]
    notes.value = noteData.notes
  } catch (e) {
    message.error(t('加载失败') + ': ' + e.message)
  } finally {
    loading.value = false
  }
}

async function onSelect(keys) {
  const key = keys[0]
  if (!key || !key.startsWith('note:')) return
  const path = key.slice(5)
  try {
    const data = await api(`/api/${props.user}/note${params({ path })}`)
    selectedNote.value = data
    selectedNoteTitle.value = data.title || path.split('/').pop().replace('.md', '')
    isAbstract.value = path.endsWith('abstract.md')
    editing.value = false
    editContent.value = data.content
  } catch (e) {
    message.error(t('读取失败') + ': ' + e.message)
  }
}

// 单篇归档能力：活跃主题目录内的非 abstract 笔记可归档；
// archive/<主题>/ 形态的可取消归档（回 topics/<主题>/）
const canArchiveSelected = computed(() => {
  if (!selectedNote.value || isAbstract.value) return false
  const p = selectedNote.value.path
  if (p.startsWith('archive/')) return false
  return !!topics.value.find(x => !x.archived && x.card &&
    p.startsWith(x.card.split('/').slice(0, -1).join('/') + '/'))
})
const canUnarchiveSelected = computed(() => {
  if (!selectedNote.value) return false
  const seg = selectedNote.value.path.split('/')
  return seg[0] === 'archive' && seg.length >= 3
})

async function archiveSelected() {
  dialog.warning({
    title: t('归档笔记'),
    content: t('将 {path} 移入 archive/<主题名>/？检索仍可用，可随时取消归档。',
      { path: selectedNote.value.path }),
    positiveText: t('归档'),
    negativeText: t('取消'),
    onPositiveClick: async () => {
      try {
        const r = await api(`/api/${props.user}/note/archive`, {
          method: 'POST',
          body: JSON.stringify({ path: selectedNote.value.path }),
        })
        message.success(t('已归档 → {dest}', { dest: r.to }))
        selectedNote.value = null
        await loadData()
      } catch (e) {
        message.error(e.message)
      }
    },
  })
}

async function unarchiveSelected() {
  try {
    const r = await api(`/api/${props.user}/note/unarchive`, {
      method: 'POST',
      body: JSON.stringify({ path: selectedNote.value.path }),
    })
    message.success(t('已取消归档 → {dest}', { dest: r.to }))
    selectedNote.value = null
    await loadData()
  } catch (e) {
    message.error(e.message)
  }
}

function toggleEdit() {
  if (editing.value && selectedNote.value) {
    // Save
    saveNote()
  } else {
    editing.value = true
  }
}

async function saveNote() {
  try {
    await api(`/api/${props.user}/note`, {
      method: 'PUT',
      body: JSON.stringify({ path: selectedNote.value.path, content: editContent.value }),
    })
    selectedNote.value.content = editContent.value
    editing.value = false
    message.success(t('已保存'))
  } catch (e) {
    message.error(t('保存失败') + ': ' + e.message)
  }
}

function handleDelete() {
  dialog.warning({
    title: t('删除笔记'),
    content: t('确认删除 {path}？git 历史可恢复。', { path: selectedNote.value.path }),
    positiveText: t('删除'),
    negativeText: t('取消'),
    onPositiveClick: async () => {
      try {
        await api(`/api/${props.user}/note${params({ path: selectedNote.value.path })}`, { method: 'DELETE' })
        message.success(t('已删除'))
        selectedNote.value = null
        await loadData()
      } catch (e) {
        message.error(t('删除失败') + ': ' + e.message)
      }
    },
  })
}

// ---- 标签管理 ----
async function tagApi(payload) {
  const data = await api(`/api/${props.user}/topics/tag`, {
    method: 'POST', body: JSON.stringify(payload),
  })
  await loadData()
  return data
}

function addTag(val) {
  if (!tagTopic.value || !val) return
  tagApi({ title: tagTopic.value, add: val }).then(() => { newTag.value = null })
}

function removeTag(title, tag) {
  tagApi({ title, remove: tag })
}

function startRename(tag) {
  renaming.value = tag
  renameInput.value = tag
}

async function doRename(old) {
  if (!renameInput.value.trim() || renameInput.value === old) {
    renaming.value = ''
    return
  }
  try {
    await api(`/api/${props.user}/topics/tag-rename`, {
      method: 'POST',
      body: JSON.stringify({ old, new: renameInput.value.trim() }),
    })
    message.success(t('已保存'))
    renaming.value = ''
    await loadData()
  } catch (e) {
    message.error(e.message)
  }
}

async function doDeleteTag(tag) {
  try {
    await api(`/api/${props.user}/topics/tag-delete`, {
      method: 'POST', body: JSON.stringify({ tag }),
    })
    message.success(t('已删除'))
    await loadData()
  } catch (e) {
    message.error(e.message)
  }
}

watch(() => props.user, () => { if (props.user) loadData() })
onMounted(() => { if (props.user) loadData() })
</script>

<style scoped>
.topics-page { height: 100%; }
.topic-tree { padding: 8px; height: 100%; overflow-y: auto; }
.topic-toolbar {
  display: flex; gap: 6px; margin-bottom: 6px;
  position: sticky; top: 0; z-index: 1;
  padding-bottom: 4px;
}
.topic-detail { padding: 0 16px; height: 100%; overflow-y: auto; }
.markdown-body { line-height: 1.7; }
.markdown-body :deep(h1) { font-size: 1.5em; margin: 0.5em 0; }
.markdown-body :deep(h2) { font-size: 1.2em; margin: 0.5em 0; }
.markdown-body :deep(code) { background: rgba(255,255,255,0.1); padding: 2px 6px; border-radius: 4px; }
.markdown-body :deep(pre) { background: rgba(0,0,0,0.2); padding: 12px; border-radius: 8px; overflow-x: auto; }
</style>
