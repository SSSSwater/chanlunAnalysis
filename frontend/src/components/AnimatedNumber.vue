<template>
  <span ref="numberRef" class="animated-number"><slot>{{ displayValue }}</slot></span>
</template>

<script setup>
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { gsap } from 'gsap'

const props = defineProps({
  value: { type: [Number, String], default: null },
  formatter: { type: Function, default: (value) => value == null ? '--' : String(value) },
  duration: { type: Number, default: 0.48 },
})

const numberRef = ref(null)
const displayValue = ref(props.formatter(props.value))
let tween

const numericValue = (value) => {
  if (value === null || value === undefined || value === '') return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

const renderValue = (value) => {
  displayValue.value = props.formatter(value)
}

watch(() => props.value, async (value, previous) => {
  const nextNumber = numericValue(value)
  const previousNumber = numericValue(previous)
  tween?.kill()

  if (nextNumber === null || previousNumber === null || nextNumber === previousNumber) {
    renderValue(value)
    return
  }

  await nextTick()
  const state = { value: previousNumber }
  tween = gsap.to(state, {
    value: nextNumber,
    duration: props.duration,
    ease: 'power2.out',
    overwrite: true,
    onUpdate: () => renderValue(state.value),
    onComplete: () => renderValue(value),
  })
}, { flush: 'post' })

onBeforeUnmount(() => tween?.kill())
</script>
