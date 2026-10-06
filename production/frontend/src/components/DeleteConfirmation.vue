<script setup lang="ts">
import { nextTick, onMounted, ref } from 'vue'

defineProps<{ title: string; name: string; pending: boolean; error: string; impact: string }>()
const emit = defineEmits<{ cancel: []; confirm: [] }>()
const cancelButton = ref<HTMLButtonElement | null>(null)
onMounted(() => { void nextTick(() => cancelButton.value?.focus()) })
</script>

<template>
  <div class="modal-mask" @click.self="!pending && emit('cancel')" @keydown.esc.prevent="!pending && emit('cancel')">
    <div class="modal" role="dialog" aria-modal="true" :aria-label="title" :aria-busy="pending">
      <div class="modal-head">{{ title }}</div>
      <div class="modal-body">
        <p>确认删除「{{ name }}」？</p>
        <p class="muted">{{ impact }}</p>
        <p v-if="error" class="form-error" role="alert">{{ error }}</p>
      </div>
      <div class="modal-foot">
        <button ref="cancelButton" type="button" :disabled="pending" @click="emit('cancel')">取消</button>
        <button type="button" class="danger" :disabled="pending" @click="emit('confirm')">{{ pending ? '删除中…' : '确认删除' }}</button>
      </div>
    </div>
  </div>
</template>
