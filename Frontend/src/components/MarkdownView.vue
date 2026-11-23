<template>
  <div class="markdown-body" v-html="html"></div>
</template>

<script setup>
import { computed } from 'vue'
import { renderMarkdown } from '../lib/markdown'

const props = defineProps({
  content: { type: String, default: '' }
})

const html = computed(() => renderMarkdown(props.content))
</script>
