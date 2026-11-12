<script setup lang="ts">
import {computed} from 'vue'
import EvidenceCitation from './EvidenceCitation.vue'
const props=defineProps<{text:string}>()
const parts=computed(()=>props.text.split(/(\[(?:P[1-9]\d*:)?E[1-9]\d*\])/g).map(t=>({text:t,ref:/^\[(?:P[1-9]\d*:)?E[1-9]\d*\]$/.test(t)?t.slice(1,-1):null})))
</script><template><span class="citation-text"><template v-for="(p,i) in parts" :key="i"><EvidenceCitation v-if="p.ref" :ref-id="p.ref"/><span v-else>{{p.text}}</span></template></span></template>
