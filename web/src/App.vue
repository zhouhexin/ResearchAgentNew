<script setup lang="ts">
import {onMounted,onUnmounted,onErrorCaptured,ref} from 'vue'
import AppLayout from './layouts/AppLayout.vue'
import EvidenceDrawer from './components/EvidenceDrawer.vue'
import {useTaskStore} from './stores/tasks'
const tasks=useTaskStore(),crash=ref('')
onMounted(()=>tasks.resume());onUnmounted(()=>tasks.stop());onErrorCaptured(e=>{crash.value=e.message;return false})
</script><template><AppLayout><div v-if="crash" class="panel state-panel" role="alert"><h2>页面暂时无法显示</h2><p>{{crash}}</p><el-button @click="crash=''">重新显示</el-button></div><RouterView v-else/><EvidenceDrawer/></AppLayout></template>
