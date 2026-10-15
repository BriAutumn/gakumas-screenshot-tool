EXTERN EnsureVersionProxy:PROC
EXTERN versionFunctions:QWORD
.code
Proxy_GetFileVersionInfoA PROC FRAME
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
call EnsureVersionProxy
mov rcx, [rsp+20h]
mov rdx, [rsp+28h]
mov r8, [rsp+30h]
mov r9, [rsp+38h]
movdqu xmm0, [rsp+40h]
movdqu xmm1, [rsp+50h]
movdqu xmm2, [rsp+60h]
movdqu xmm3, [rsp+70h]
add rsp, 0A8h
jmp QWORD PTR [versionFunctions+0]
Proxy_GetFileVersionInfoA ENDP
Proxy_GetFileVersionInfoByHandle PROC FRAME
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
call EnsureVersionProxy
mov rcx, [rsp+20h]
mov rdx, [rsp+28h]
mov r8, [rsp+30h]
mov r9, [rsp+38h]
movdqu xmm0, [rsp+40h]
movdqu xmm1, [rsp+50h]
movdqu xmm2, [rsp+60h]
movdqu xmm3, [rsp+70h]
add rsp, 0A8h
jmp QWORD PTR [versionFunctions+8]
Proxy_GetFileVersionInfoByHandle ENDP
Proxy_GetFileVersionInfoExA PROC FRAME
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
call EnsureVersionProxy
mov rcx, [rsp+20h]
mov rdx, [rsp+28h]
mov r8, [rsp+30h]
mov r9, [rsp+38h]
movdqu xmm0, [rsp+40h]
movdqu xmm1, [rsp+50h]
movdqu xmm2, [rsp+60h]
movdqu xmm3, [rsp+70h]
add rsp, 0A8h
jmp QWORD PTR [versionFunctions+16]
Proxy_GetFileVersionInfoExA ENDP
Proxy_GetFileVersionInfoExW PROC FRAME
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
call EnsureVersionProxy
mov rcx, [rsp+20h]
mov rdx, [rsp+28h]
mov r8, [rsp+30h]
mov r9, [rsp+38h]
movdqu xmm0, [rsp+40h]
movdqu xmm1, [rsp+50h]
movdqu xmm2, [rsp+60h]
movdqu xmm3, [rsp+70h]
add rsp, 0A8h
jmp QWORD PTR [versionFunctions+24]
Proxy_GetFileVersionInfoExW ENDP
Proxy_GetFileVersionInfoSizeA PROC FRAME
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
call EnsureVersionProxy
mov rcx, [rsp+20h]
mov rdx, [rsp+28h]
mov r8, [rsp+30h]
mov r9, [rsp+38h]
movdqu xmm0, [rsp+40h]
movdqu xmm1, [rsp+50h]
movdqu xmm2, [rsp+60h]
movdqu xmm3, [rsp+70h]
add rsp, 0A8h
jmp QWORD PTR [versionFunctions+32]
Proxy_GetFileVersionInfoSizeA ENDP
Proxy_GetFileVersionInfoSizeExA PROC FRAME
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
call EnsureVersionProxy
mov rcx, [rsp+20h]
mov rdx, [rsp+28h]
mov r8, [rsp+30h]
mov r9, [rsp+38h]
movdqu xmm0, [rsp+40h]
movdqu xmm1, [rsp+50h]
movdqu xmm2, [rsp+60h]
movdqu xmm3, [rsp+70h]
add rsp, 0A8h
jmp QWORD PTR [versionFunctions+40]
Proxy_GetFileVersionInfoSizeExA ENDP
Proxy_GetFileVersionInfoSizeExW PROC FRAME
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
call EnsureVersionProxy
mov rcx, [rsp+20h]
mov rdx, [rsp+28h]
mov r8, [rsp+30h]
mov r9, [rsp+38h]
movdqu xmm0, [rsp+40h]
movdqu xmm1, [rsp+50h]
movdqu xmm2, [rsp+60h]
movdqu xmm3, [rsp+70h]
add rsp, 0A8h
jmp QWORD PTR [versionFunctions+48]
Proxy_GetFileVersionInfoSizeExW ENDP
Proxy_GetFileVersionInfoSizeW PROC FRAME
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
call EnsureVersionProxy
mov rcx, [rsp+20h]
mov rdx, [rsp+28h]
mov r8, [rsp+30h]
mov r9, [rsp+38h]
movdqu xmm0, [rsp+40h]
movdqu xmm1, [rsp+50h]
movdqu xmm2, [rsp+60h]
movdqu xmm3, [rsp+70h]
add rsp, 0A8h
jmp QWORD PTR [versionFunctions+56]
Proxy_GetFileVersionInfoSizeW ENDP
Proxy_GetFileVersionInfoW PROC FRAME
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
call EnsureVersionProxy
mov rcx, [rsp+20h]
mov rdx, [rsp+28h]
mov r8, [rsp+30h]
mov r9, [rsp+38h]
movdqu xmm0, [rsp+40h]
movdqu xmm1, [rsp+50h]
movdqu xmm2, [rsp+60h]
movdqu xmm3, [rsp+70h]
add rsp, 0A8h
jmp QWORD PTR [versionFunctions+64]
Proxy_GetFileVersionInfoW ENDP
Proxy_VerFindFileA PROC FRAME
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
call EnsureVersionProxy
mov rcx, [rsp+20h]
mov rdx, [rsp+28h]
mov r8, [rsp+30h]
mov r9, [rsp+38h]
movdqu xmm0, [rsp+40h]
movdqu xmm1, [rsp+50h]
movdqu xmm2, [rsp+60h]
movdqu xmm3, [rsp+70h]
add rsp, 0A8h
jmp QWORD PTR [versionFunctions+72]
Proxy_VerFindFileA ENDP
Proxy_VerFindFileW PROC FRAME
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
call EnsureVersionProxy
mov rcx, [rsp+20h]
mov rdx, [rsp+28h]
mov r8, [rsp+30h]
mov r9, [rsp+38h]
movdqu xmm0, [rsp+40h]
movdqu xmm1, [rsp+50h]
movdqu xmm2, [rsp+60h]
movdqu xmm3, [rsp+70h]
add rsp, 0A8h
jmp QWORD PTR [versionFunctions+80]
Proxy_VerFindFileW ENDP
Proxy_VerInstallFileA PROC FRAME
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
call EnsureVersionProxy
mov rcx, [rsp+20h]
mov rdx, [rsp+28h]
mov r8, [rsp+30h]
mov r9, [rsp+38h]
movdqu xmm0, [rsp+40h]
movdqu xmm1, [rsp+50h]
movdqu xmm2, [rsp+60h]
movdqu xmm3, [rsp+70h]
add rsp, 0A8h
jmp QWORD PTR [versionFunctions+88]
Proxy_VerInstallFileA ENDP
Proxy_VerInstallFileW PROC FRAME
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
call EnsureVersionProxy
mov rcx, [rsp+20h]
mov rdx, [rsp+28h]
mov r8, [rsp+30h]
mov r9, [rsp+38h]
movdqu xmm0, [rsp+40h]
movdqu xmm1, [rsp+50h]
movdqu xmm2, [rsp+60h]
movdqu xmm3, [rsp+70h]
add rsp, 0A8h
jmp QWORD PTR [versionFunctions+96]
Proxy_VerInstallFileW ENDP
Proxy_VerLanguageNameA PROC FRAME
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
call EnsureVersionProxy
mov rcx, [rsp+20h]
mov rdx, [rsp+28h]
mov r8, [rsp+30h]
mov r9, [rsp+38h]
movdqu xmm0, [rsp+40h]
movdqu xmm1, [rsp+50h]
movdqu xmm2, [rsp+60h]
movdqu xmm3, [rsp+70h]
add rsp, 0A8h
jmp QWORD PTR [versionFunctions+104]
Proxy_VerLanguageNameA ENDP
Proxy_VerLanguageNameW PROC FRAME
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
call EnsureVersionProxy
mov rcx, [rsp+20h]
mov rdx, [rsp+28h]
mov r8, [rsp+30h]
mov r9, [rsp+38h]
movdqu xmm0, [rsp+40h]
movdqu xmm1, [rsp+50h]
movdqu xmm2, [rsp+60h]
movdqu xmm3, [rsp+70h]
add rsp, 0A8h
jmp QWORD PTR [versionFunctions+112]
Proxy_VerLanguageNameW ENDP
Proxy_VerQueryValueA PROC FRAME
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
call EnsureVersionProxy
mov rcx, [rsp+20h]
mov rdx, [rsp+28h]
mov r8, [rsp+30h]
mov r9, [rsp+38h]
movdqu xmm0, [rsp+40h]
movdqu xmm1, [rsp+50h]
movdqu xmm2, [rsp+60h]
movdqu xmm3, [rsp+70h]
add rsp, 0A8h
jmp QWORD PTR [versionFunctions+120]
Proxy_VerQueryValueA ENDP
Proxy_VerQueryValueW PROC FRAME
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
call EnsureVersionProxy
mov rcx, [rsp+20h]
mov rdx, [rsp+28h]
mov r8, [rsp+30h]
mov r9, [rsp+38h]
movdqu xmm0, [rsp+40h]
movdqu xmm1, [rsp+50h]
movdqu xmm2, [rsp+60h]
movdqu xmm3, [rsp+70h]
add rsp, 0A8h
jmp QWORD PTR [versionFunctions+128]
Proxy_VerQueryValueW ENDP
END