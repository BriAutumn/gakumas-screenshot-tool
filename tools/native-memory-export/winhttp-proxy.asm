; tools/native-memory-export/winhttp-proxy.asm
;
; x64 forwarding thunks for the winhttp.dll coexistence proxy.
;
; Every exported "WinHttp*" entry is a small thunk that:
;   1. reserves a 0xA8-byte frame (0xA8 mod 16 == 8, so rsp stays 16-aligned
;      for the outgoing call while leaving the incoming stack arguments above
;      rsp untouched),
;   2. spills the integer argument registers rcx/rdx/r8/r9 and vector argument
;      registers xmm0-xmm3,
;   3. runs the lazy once-initializer (which preserves the caller's LastError),
;   4. restores the argument registers,
;   5. tail-jumps to the real System32\winhttp.dll export through the shared
;      winhttpFunctions pointer table.
;
; The tail jump preserves the caller's 5th+ (stack) arguments because rsp is
; restored to its entry value and nothing below the return address is clobbered.
; The frame is fully described (.allocstack/.endprolog) so the unwinder can walk
; through these thunks.
;
; A single MASM macro generates every thunk so the index/name mapping stays in
; lockstep with winhttpFunctions[] in winhttp-proxy.cpp and winhttp.def.

EXTERN EnsureWinHttpProxy:PROC
EXTERN winhttpFunctions:QWORD

PROXY_THUNK MACRO exportName, slot
exportName PROC FRAME
    sub rsp, 0A8h
    .allocstack 0A8h
    .endprolog
    mov [rsp+20h], rcx
    mov [rsp+28h], rdx
    mov [rsp+30h], r8
    mov [rsp+38h], r9
    movdqu [rsp+40h], xmm0
    movdqu [rsp+50h], xmm1
    movdqu [rsp+60h], xmm2
    movdqu [rsp+70h], xmm3
    call EnsureWinHttpProxy
    mov rcx, [rsp+20h]
    mov rdx, [rsp+28h]
    mov r8,  [rsp+30h]
    mov r9,  [rsp+38h]
    movdqu xmm0, [rsp+40h]
    movdqu xmm1, [rsp+50h]
    movdqu xmm2, [rsp+60h]
    movdqu xmm3, [rsp+70h]
    add rsp, 0A8h
    jmp QWORD PTR [winhttpFunctions + (slot * 8)]
exportName ENDP
ENDM

.code

; ---- Baseline WinHTTP API (also the UnityPlayer import set) ---------------
PROXY_THUNK Proxy_WinHttpAddRequestHeaders,               0
PROXY_THUNK Proxy_WinHttpCheckPlatform,                   1
PROXY_THUNK Proxy_WinHttpCloseHandle,                     2
PROXY_THUNK Proxy_WinHttpConnect,                         3
PROXY_THUNK Proxy_WinHttpCrackUrl,                        4
PROXY_THUNK Proxy_WinHttpCreateProxyResolver,             5
PROXY_THUNK Proxy_WinHttpCreateUrl,                       6
PROXY_THUNK Proxy_WinHttpDetectAutoProxyConfigUrl,        7
PROXY_THUNK Proxy_WinHttpFreeProxyResult,                 8
PROXY_THUNK Proxy_WinHttpGetDefaultProxyConfiguration,    9
PROXY_THUNK Proxy_WinHttpGetIEProxyConfigForCurrentUser, 10
PROXY_THUNK Proxy_WinHttpGetProxyForUrl,                 11
PROXY_THUNK Proxy_WinHttpGetProxyForUrlEx,               12
PROXY_THUNK Proxy_WinHttpGetProxyResult,                 13
PROXY_THUNK Proxy_WinHttpOpen,                           14
PROXY_THUNK Proxy_WinHttpOpenRequest,                    15
PROXY_THUNK Proxy_WinHttpQueryAuthSchemes,               16
PROXY_THUNK Proxy_WinHttpQueryDataAvailable,             17
PROXY_THUNK Proxy_WinHttpQueryHeaders,                   18
PROXY_THUNK Proxy_WinHttpQueryOption,                    19
PROXY_THUNK Proxy_WinHttpReadData,                       20
PROXY_THUNK Proxy_WinHttpReceiveResponse,                21
PROXY_THUNK Proxy_WinHttpResetAutoProxy,                 22
PROXY_THUNK Proxy_WinHttpSendRequest,                    23
PROXY_THUNK Proxy_WinHttpSetCredentials,                 24
PROXY_THUNK Proxy_WinHttpSetDefaultProxyConfiguration,   25
PROXY_THUNK Proxy_WinHttpSetOption,                      26
PROXY_THUNK Proxy_WinHttpSetStatusCallback,              27
PROXY_THUNK Proxy_WinHttpSetTimeouts,                    28
PROXY_THUNK Proxy_WinHttpTimeFromSystemTime,             29
PROXY_THUNK Proxy_WinHttpTimeToSystemTime,               30
PROXY_THUNK Proxy_WinHttpWriteData,                      31

END
