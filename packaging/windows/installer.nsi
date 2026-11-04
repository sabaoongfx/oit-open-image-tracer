; SPDX-License-Identifier: GPL-3.0-or-later
; NSIS installer for Open Image Tracer. Defines passed by build.sh:
;   VERSION, SRC (PyInstaller folder), ICON, LICENSE, OUTFILE
Unicode true
!include "MUI2.nsh"

Name "Open Image Tracer"
OutFile "${OUTFILE}"
InstallDir "$PROGRAMFILES64\Open Image Tracer"
InstallDirRegKey HKLM "Software\OpenImageTracer" "InstallDir"
RequestExecutionLevel admin
SetCompressor /SOLID lzma

!define MUI_ICON "${ICON}"
!define MUI_UNICON "${ICON}"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\OpenImageTracer"

!insertmacro MUI_PAGE_LICENSE "${LICENSE}"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\open-image-tracer.exe"
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Section "Open Image Tracer"
  SetOutPath "$INSTDIR"
  File /r "${SRC}\*.*"
  CreateShortcut "$SMPROGRAMS\Open Image Tracer.lnk" "$INSTDIR\open-image-tracer.exe"
  WriteUninstaller "$INSTDIR\uninstall.exe"
  WriteRegStr HKLM "Software\OpenImageTracer" "InstallDir" "$INSTDIR"
  WriteRegStr HKLM "${UNINST_KEY}" "DisplayName" "Open Image Tracer"
  WriteRegStr HKLM "${UNINST_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKLM "${UNINST_KEY}" "Publisher" "Sabaoon"
  WriteRegStr HKLM "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\open-image-tracer.exe"
  WriteRegStr HKLM "${UNINST_KEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegDWORD HKLM "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKLM "${UNINST_KEY}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  Delete "$SMPROGRAMS\Open Image Tracer.lnk"
  RMDir /r "$INSTDIR"
  DeleteRegKey HKLM "${UNINST_KEY}"
  DeleteRegKey HKLM "Software\OpenImageTracer"
SectionEnd
