<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { ArrowLeft, ArrowRight, Film, Play, Save, Trash2, Undo2 } from 'lucide-vue-next'
import { api } from '@/lib/api'
type Clip = { asset:string; start:number; end:number; volume:number }
type Cut = { revision:string; clips:Clip[]; reason:string; timing_warning:boolean; assets:Record<string,{name:string;duration:number;url:string}> }
const props = defineProps<{taskId:string; locked:boolean}>()
const emit = defineEmits<{saved:[]; dirty:[value:boolean]; advanced:[]; assistant:[kind:string]}>()
const data = ref<Cut>(), clips = ref<Clip[]>([]), selected = ref(0), player = ref<HTMLVideoElement>()
const busy = ref(false), error = ref(''), dirty = ref(false), playing = ref(false)
const closeGaps = ref(false)
let disposed = false
const current = computed(()=>clips.value[selected.value])
const total = computed(()=>clips.value.reduce((sum,c)=>sum+c.end-c.start,0))
const valid = computed(()=>clips.value.length>0 && clips.value.every(c=>Number.isFinite(c.start)&&Number.isFinite(c.end)&&c.start>=0&&c.end>c.start&&c.end<= (data.value?.assets[c.asset]?.duration || 0)+.02))
const time = (n:number)=>`${Math.floor(n/60).toString().padStart(2,'0')}:${(n%60).toFixed(1).padStart(4,'0')}`
function changed() { dirty.value=true; emit('dirty',true); playing.value=false; player.value?.pause();if(player.value&&current.value)player.value.volume=current.value.volume }
function choose(index:number) { playing.value=false; player.value?.pause();selected.value=index }
function move(index:number,delta:number) {
  const target=index+delta
  if(target<0||target>=clips.value.length) return
  const [clip]=clips.value.splice(index,1); if(clip) clips.value.splice(target,0,clip)
  selected.value=target;changed()
}
function remove(index:number) {clips.value.splice(index,1);selected.value=Math.min(selected.value,clips.value.length-1);changed()}
function reset() { player.value?.pause();clips.value=structuredClone(data.value ? JSON.parse(JSON.stringify(data.value.clips)) : []);selected.value=0;dirty.value=false;emit('dirty',false);playing.value=false }
function ready() {if(player.value&&current.value){player.value.currentTime=current.value.start;player.value.volume=current.value.volume;if(playing.value) void player.value.play().catch(()=>{playing.value=false})}}
function tick() {
  if(!player.value||!current.value) return
  if(player.value.currentTime>=current.value.end-.025) {
    player.value.pause()
    if(playing.value&&selected.value<clips.value.length-1) {selected.value++;return}
    playing.value=false
  }
}
async function preview() {if(!valid.value)return;player.value?.pause();playing.value=true;selected.value=0;await nextTick();ready()}
async function save() {
  if(!data.value||!dirty.value) return
  busy.value=true;error.value=''
  try {const value=await api<Cut>(`/video-replicas/${props.taskId}/production/cut`,{method:'PUT',body:JSON.stringify({revision:data.value.revision,clips:clips.value,close_gaps:closeGaps.value})});if(disposed)return;data.value=value;reset();emit('saved')}
  catch(e){error.value=e instanceof Error?e.message:'保存失败'}finally{busy.value=false}
}
onMounted(async()=>{try{const value=await api<Cut>(`/video-replicas/${props.taskId}/production/cut`);if(!disposed){data.value=value;reset()}}catch(e){error.value=String(e)}})
onBeforeUnmount(()=>{disposed=true;player.value?.pause()})
</script>

<template>
  <section class="cut-editor" aria-label="视频剪辑">
    <div class="cut-top"><div><h3>把片段剪成你的作品</h3><p>选择片段，调整保留区间和原声音量。保存后导出即可生效。</p></div><span>{{ clips.length }} 段 · {{ time(total) }}</span></div>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="!data&&!error" role="status">正在读取片段…</p>
    <div v-if="data?.reason" class="cut-empty"><Film :size="28"/><p>{{ data.reason }}</p><button @click="emit('advanced')">打开高级编辑</button></div>
    <template v-else-if="data">
      <label v-if="data.timing_warning"><input v-model="closeGaps" type="checkbox" @change="changed"/>原工程有片段间隔或重叠。保存时按当前顺序紧密拼接（顺序预览也按此方式播放）。</label>
      <div class="cut-layout"><div class="cut-preview"><video v-if="current" :key="selected+current.asset" ref="player" :src="data.assets[current.asset]?.url" controls playsinline preload="metadata" @loadedmetadata="ready" @timeupdate="tick" @ended="tick"/><div class="cut-preview-footer"><span>片段 {{ selected+1 }} · {{ current ? time(current.end-current.start) : '未选择' }}</span><button :disabled="!valid" @click="preview"><Play :size="16"/>顺序预览</button></div></div>
      <aside class="cut-inspector"><h4>片段设置</h4><fieldset :disabled="busy||locked||!current"><template v-if="current"><label>保留起点（秒）<input v-model.number="current.start" type="number" min="0" :max="current.end-.034" step="0.1" @input="changed"/></label><label>保留终点（秒）<input v-model.number="current.end" type="number" :min="current.start+.034" :max="data.assets[current.asset]?.duration" step="0.1" @input="changed"/></label><label>原声音量 · {{ Math.round(current.volume*100) }}%<input v-model.number="current.volume" type="range" min="0" max="1" step=".05" @input="changed"/></label><small>原片 {{ time(data.assets[current.asset]?.duration || 0) }} · 0% 为静音</small></template></fieldset><p v-if="locked">请先关闭高级编辑器再剪辑。</p><div class="cut-shortcuts"><button :disabled="dirty" @click="emit('assistant','captions')">添加对白字幕</button><button :disabled="dirty" @click="emit('assistant','audio')">音乐 / 旁白</button><button :disabled="dirty" @click="emit('assistant','titles')">添加标题</button></div><small v-if="dirty">先保存剪辑，再添加字幕或音乐。</small></aside></div>
      <div class="cut-timeline" aria-label="片段时间轴"><article v-for="(clip,index) in clips" :key="index" :class="{selected:selected===index}" :style="{flexGrow:Math.max(1,clip.end-clip.start)}"><button class="cut-select" :aria-pressed="selected===index" @click="choose(index)"><Film :size="20"/><strong>{{ index+1 }} · {{ data.assets[clip.asset]?.name }}</strong><small>{{ time(clip.start) }} — {{ time(clip.end) }}</small></button><div class="cut-row-actions"><button :disabled="busy||locked||index===0" aria-label="前移片段" @click="move(index,-1)"><ArrowLeft :size="16"/></button><button :disabled="busy||locked||index===clips.length-1" aria-label="后移片段" @click="move(index,1)"><ArrowRight :size="16"/></button><button :disabled="busy||locked||clips.length===1" aria-label="移除片段" @click="remove(index)"><Trash2 :size="16"/></button></div></article></div>
      <div class="cut-footer"><small>{{ dirty?'有未保存的剪辑':'已保存到当前工程' }} · 移除片段不会删除原素材</small><div><button :disabled="!dirty||busy" @click="reset"><Undo2 :size="16"/>撤销未保存修改</button><button :disabled="!dirty||!valid||busy||locked" @click="save"><Save :size="16"/>{{ busy?'校验并保存中…':'保存剪辑' }}</button></div></div>
    </template>
  </section>
</template>

<style scoped>
.cut-editor{font-variant-numeric:tabular-nums;min-width:0}.cut-top,.cut-footer,.cut-preview-footer{display:flex;justify-content:space-between;align-items:center;gap:16px}.cut-top{margin:20px 0}.cut-top h3{margin:0;font-size:21px;letter-spacing:-.4px}.cut-top p,.cut-editor small{font-size:12px;color:var(--ink-secondary)}.cut-top span{white-space:nowrap;font-size:13px}.cut-layout{display:grid;grid-template-columns:minmax(0,1fr) 250px;gap:20px}.cut-preview{min-width:0;padding:8px;background:var(--glass-inset);border-radius:20px}.cut-preview video{display:block;width:100%;aspect-ratio:16/9;max-height:52dvh;background:#101114;border-radius:12px;object-fit:contain}.cut-preview-footer{padding:8px;font-size:12px}.cut-inspector{padding:20px;background:var(--glass-inset);border-radius:20px;min-width:0}.cut-inspector h4{margin:0 0 16px}.cut-inspector fieldset{border:0;padding:0;display:grid;gap:14px;min-width:0}.cut-inspector label{display:grid;gap:8px;font-size:12px}.cut-inspector input[type=number]{width:100%;box-sizing:border-box;padding:10px;min-height:44px;background:var(--glass-inset);border:1px solid var(--glass-edge);border-radius:10px;color:var(--ink);font:inherit}.cut-inspector input[type=range]{width:100%;min-height:40px;accent-color:var(--brand)}.cut-shortcuts{display:grid;gap:8px;margin-top:18px}.cut-timeline{display:flex;gap:10px;overflow-x:auto;padding:12px 2px;margin:14px 0;overscroll-behavior-x:contain}.cut-timeline article{min-width:155px;max-width:280px;padding:6px;background:var(--glass-inset);border-radius:16px;box-shadow:0 0 0 1px var(--glass-edge)}.cut-timeline article.selected{box-shadow:0 0 0 2px var(--brand)}.cut-editor button{min-width:44px;min-height:44px;cursor:pointer}.cut-select{display:grid!important;justify-items:start!important;text-align:left;width:100%;background:transparent!important}.cut-select strong{font-size:12px}.cut-row-actions{display:flex;justify-content:flex-end;gap:4px}.cut-row-actions button{padding:8px!important}.cut-footer>div{display:flex;gap:8px;flex-wrap:wrap}.cut-empty{padding:30px;text-align:center}.cut-editor button:focus-visible{outline:2px solid var(--brand);outline-offset:3px}@media(max-width:760px){.cut-layout{grid-template-columns:minmax(0,1fr)}.cut-top,.cut-footer{align-items:flex-start;flex-direction:column}.cut-inspector{padding:16px}.cut-inspector fieldset{grid-template-columns:1fr 1fr}.cut-shortcuts{grid-template-columns:repeat(3,minmax(0,1fr))}.cut-shortcuts button{font-size:11px;padding:8px}.cut-footer>div{width:100%}.cut-footer>div button{flex:1}}
</style>

<style scoped>
.cut-editor button{display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:10px 14px;background:var(--glass-inset);color:var(--ink);border:0;border-radius:12px;font:inherit;font-size:13px;transition:transform .15s,background .15s}
.cut-editor button:active{transform:scale(.96)}
.cut-editor button:disabled{opacity:.45;cursor:default}
.cut-editor button:hover:not(:disabled){background:var(--glass-edge)}
.cut-select{gap:6px!important;padding:12px!important}
.cut-footer>div button:last-child{background:var(--brand);color:white}
.cut-editor>label{display:flex;align-items:flex-start;gap:8px;font-size:12px;line-height:1.7;margin:12px 0;color:var(--ink-secondary)}
.cut-editor>label input{margin-top:4px;accent-color:var(--brand);flex-shrink:0}
@media(max-width:760px){.cut-inspector fieldset>label:nth-child(3),.cut-inspector fieldset>small{grid-column:1/-1}.cut-shortcuts button{font-size:11px;padding:8px}}
</style>
