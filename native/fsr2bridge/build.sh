#!/bin/sh
# Збірка під Windows x64 (MinGW). Результат: out/dinput8.dll
set -e
cd "$(dirname "$0")"
CC=${CC:-x86_64-w64-mingw32-gcc}
mkdir -p out
MH=third_party/minhook
$CC -O2 -s -shared -Wall -Wno-unused-function -o out/dinput8.dll ${SRC:-diag2.c} dinput8.def \
  $MH/src/hook.c $MH/src/buffer.c $MH/src/trampoline.c $MH/src/hde/hde64.c \
  -D__REQUIRED_RPCNDR_H_VERSION__=475 -D__REQUIRED_RPCSAL_H_VERSION__=100 -Ithird_party/directx -I$MH/include -static-libgcc -Wl,--enable-stdcall-fixup
ls -la out/dinput8.dll
