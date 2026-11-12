<script setup lang="ts">
import {computed} from 'vue'
import {Document,Position,Connection,Clock,Setting,ArrowRight,Search,Collection,ChatDotRound,Reading,Files,Odometer,Menu,InfoFilled,Plus,Refresh,ChatLineRound} from '@element-plus/icons-vue'
const props=defineProps<{name:string}>()
const icons={Document,Position,Connection,Clock,Setting,ArrowRight,Search,Collection,ChatDotRound,Reading,Files,Odometer,Menu,InfoFilled,Plus,Refresh,ChatLineRound}
const icon=computed(()=>icons[props.name as keyof typeof icons]||Document)
</script><template><el-icon aria-hidden="true"><component :is="icon"/></el-icon></template>
