<script setup lang="ts">
defineProps<{
  label?: string
  kind?: 'plain' | 'warn' | 'danger' | 'ok'
}>()
</script>

<template>
  <div class="note" :class="kind ?? 'plain'">
    <div class="note-body">
      <div v-if="label" class="note-label">{{ label }}</div>
      <slot />
    </div>
  </div>
</template>
