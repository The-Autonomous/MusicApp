# Native Tcl/Tk presentation. Geometry, colours and fonts mirror the Python source.
# Rust owns playback, persistence, global keyboard/mouse hooks and native window styles.
package require Tk 8.6
wm withdraw .
namespace eval ::music {
    variable colors [dict create window_bg #1d1f24 frame_bg #23272e entry_bg #2c313a text #e0e0e0 text_dark #aaaaaa placeholder #888888 accent #00ffd5 accent_active #00bfa5 danger #ff4d4d danger_active #ff1a1a warning #ffae42 warning_active #ff8c00 button #555555 button_active #777777 border #555555 transparent #ff00ff]
    variable title {}; variable duration {}; variable lyric {}; variable lyrics_visible 1
    variable position {}; variable layout_key {}; variable overlay_visible 0; variable overlay_allowed 0
    variable native_mouse_hooks 0; variable overlay_dragging 0; variable drag_pulse_timer {}
    variable hint_opacity 0.0; variable hint_fade_timer {}; variable hint_hide_timer {}
    variable authentication_active 0; variable authentication_windows {}
    variable search_term {}; variable use_youtube 1; variable permanent 0
    variable search_results {}; variable searching 0; variable downloading 0
    variable search_timer {}; variable spinner_timer {}; variable record_timer {}
    variable restore_overlay 0; variable gaming 0; variable accept_radio_eq 0
    variable volume 50; variable delay 35; variable wet 0; variable preset Flat
    variable source {YouTube Music liked songs}; variable hosting 0; variable auto_gaming 1
    variable yt_message {Connect YouTube to start listening.}; variable yt_phase idle
    variable yt_connected 0; variable yt_ready 0; variable yt_busy 0; variable yt_startup 0
    variable yt_saved 0; variable yt_total {}; variable yt_collapsed 0; variable yt_geometry {}
    variable yt_signature {}; variable yt_timer {}; variable controls_tab 0; variable edit_action {}
    variable yt_layout_signature {}; variable yt_progress_mode {}; variable yt_progress_max {}; variable yt_progress_value {}
    variable yt_download_signature {}; variable yt_reports {}; variable yt_frame_visible 0; variable yt_rows
    variable status {}; variable knob
    variable groups {{Playback {skip_previous pause_play skip_next volume_down repeat_toggle volume_up}} {{Find & tune} {show_search show_eq_menu youtube_sync}} {Player {lyrics_toggle_visibility toggle_overlay player_on_off}} {Radio {radio_enable_toggle radio_scan_station}} {App {show_hints player_restart kill_self}}}
    variable labels [dict create skip_previous {Previous song} skip_next {Next song} pause_play {Play / pause} volume_down {Volume down} volume_up {Volume up} repeat_toggle Repeat show_search {Find a song} show_eq_menu {Sound & effects} youtube_sync YouTube lyrics_toggle_visibility {Show / hide lyrics} toggle_overlay {Show / hide player} player_on_off {Player on / off} radio_enable_toggle {Radio on / off} radio_scan_station {Find a station} show_hints {Open controls} player_restart {Restart app} kill_self {Quit app}]
    variable chords [dict create skip_previous {alt -} skip_next {alt =} volume_down {alt [} volume_up {alt ]} pause_play {alt ;} repeat_toggle {alt '} lyrics_toggle_visibility {alt /} radio_enable_toggle {alt a} radio_scan_station {alt `} show_search {alt \\} show_eq_menu {{right alt} \\} youtube_sync {{right alt} y} toggle_overlay {alt shift} player_on_off {{right alt} {right shift}} show_hints {alt {right alt}} player_restart {{right alt} .} kill_self {{right alt} ctrl}]
    variable default_chords $chords
    variable alt_required
    dict for {id chord} $chords { set alt_required($id) 1 }
    variable presets [dict create]
}
proc ::music::c {key} {variable colors; return [dict get $colors $key]}
proc ::music::emit {action args} {rust_action $action {*}$args}
proc ::music::exists {w} {winfo exists $w}
proc ::music::center {w width height} {
    wm geometry $w ${width}x${height}+[expr {max(0,([winfo screenwidth $w]-$width)/2)}]+[expr {max(0,([winfo screenheight $w]-$height)/2)}]
}
proc ::music::card {w title} {
    toplevel $w -bg [c window_bg]; wm withdraw $w; wm title $w $title
    wm overrideredirect $w 1; wm attributes $w -topmost 1
}
proc ::music::drag_start {w x y} {
    if {$w eq ".overlay"} {
        overlay_drag_begin
        variable native_mouse_hooks
        if {$native_mouse_hooks} {return}
    }
    variable drag; set drag($w) [list $x $y [winfo x $w] [winfo y $w]]
}
proc ::music::drag_move {w x y} {
    variable native_mouse_hooks
    if {$w eq ".overlay" && $native_mouse_hooks} {return}
    variable drag
    if {[info exists drag($w)]} {
        lassign $drag($w) sx sy wx wy
        set next_x [expr {$wx+$x-$sx}]; set next_y [expr {$wy+$y-$sy}]
        if {$w eq ".overlay"} {place_overlay $next_x $next_y} else {wm geometry $w [format %+d%+d $next_x $next_y]}
    }
}
proc ::music::drag_handle {widget window {button 1}} {
    $widget configure -cursor fleur
    bind $widget <Button-$button> [list ::music::drag_start $window %X %Y]
    bind $widget <B$button-Motion> [list ::music::drag_move $window %X %Y]
}
proc ::music::card_button {w text command {primary 0} args} {
    button $w -text $text -command $command -font {{Segoe UI} 10} -bg [c [expr {$primary ? "accent" : "frame_bg"}]] -fg [c [expr {$primary ? "window_bg" : "text"}]] -activebackground [c accent_active] -relief flat -bd 0 -padx 13 -pady 8 -cursor hand2 -takefocus 1 {*}$args
    return $w
}
proc ::music::init {} {
    ttk::style theme use clam
    ttk::style configure TFrame -background [c frame_bg]
    ttk::style configure Accent.TFrame -background [c window_bg]
    ttk::style configure TLabel -background [c frame_bg] -foreground [c text] -font {{Segoe UI} 13}
    ttk::style configure Header.TLabel -foreground [c accent] -font {{Segoe UI} 20 bold}
    ttk::style configure Status.TLabel -foreground [c warning] -font {{Segoe UI} 10 italic}
    ttk::style configure TButton -font {{Segoe UI} 13} -background [c button] -foreground [c text] -relief flat -padding 8 -borderwidth 0
    ttk::style map TButton -background [list active [c button_active]]
    foreach {style color active fg} {Accent accent accent_active frame_bg Danger danger danger_active text Warning warning warning_active window_bg} {
        ttk::style configure $style.TButton -background [c $color] -foreground [c $fg]
        ttk::style map $style.TButton -background [list active [c $active]]
    }
    ttk::style configure TCheckbutton -background [c window_bg] -foreground [c text] -relief flat -indicatorcolor [c entry_bg] -font {{Segoe UI} 13}
    ttk::style map TCheckbutton -indicatorcolor [list selected [c accent]] -background [list active [c frame_bg]]
    ttk::style configure TEntry -fieldbackground [c entry_bg] -foreground [c text] -insertcolor [c text] -bordercolor [c border] -relief flat -padding 5
    ttk::style map TEntry -bordercolor [list focus [c accent]] -fieldbackground [list !disabled [c entry_bg]]
    ttk::style configure TCombobox -fieldbackground [c entry_bg] -background [c entry_bg] -foreground [c text] -arrowcolor [c text_dark] -bordercolor [c border] -selectbackground [c entry_bg] -selectforeground [c text] -font {{Segoe UI} 12} -padding 5
    ttk::style map TCombobox -fieldbackground [list readonly [c entry_bg]] -foreground [list readonly [c text]] -bordercolor [list focus [c accent]]
    ttk::style configure TMenubutton -background [c entry_bg] -foreground [c text] -relief flat -padding {8 4 12 4}
    ttk::style map TMenubutton -background [list active [c button]] -arrowcolor [list active [c accent] !disabled [c text_dark]]
    ttk::style configure MusicApp.Horizontal.TProgressbar -troughcolor [c frame_bg] -background [c accent] -borderwidth 0 -thickness 5
    foreach {name size options} {main 14 bold time 12 {} lyrics 11 italic hint 9 {}} {
        font create ::music::font_$name -family {Segoe UI} -size $size
        if {$options eq "bold"} {font configure ::music::font_$name -weight bold}
        if {$options eq "italic"} {font configure ::music::font_$name -slant italic}
    }
}
proc ::music::wrap {text maxchars} {
    if {[string trim $text] eq ""} {return {}}
    set lines {}; set line {}
    foreach word [regexp -all -inline {\S+} $text] {
        if {$line eq ""} {set line $word} elseif {[string length $line]+1+[string length $word]<=$maxchars} {append line " " $word} else {lappend lines $line; set line $word}
    }
    if {$line ne ""} {lappend lines $line}
    if {[llength $lines]>2} {
        set middle [expr {[string length $text]/2}]
        set split [string last " " [string range $text 0 [expr {$middle+[string length $text]/4-1}]]]
        if {$split<0} {set split $middle}
        set line1 [string trim [string range $text 0 [expr {$split-1}]]]
        set line2 [string trim [string range $text $split end]]
        return [expr {$line2 ne "" ? "$line1\n$line2" : $line1}]
    }
    return [join $lines \n]
}
proc ::music::rounded {canvas width height radius} {
    set points {}; set steps [expr {max($radius,10)}]; set curve {}
    for {set i 0} {$i<=$steps} {incr i} {
        set angle [expr {acos(-1)*($i/double($steps))*.5}]
        lappend curve [list [expr {(cos($angle)-1)*$radius}] [expr {(sin($angle)-1)*$radius}]]
    }
    foreach pair $curve {lassign $pair co si; lappend points [expr {$width+$si}] [expr {-$co}]}
    foreach pair $curve {lassign $pair co si; lappend points [expr {$width+$co}] [expr {$height+$si}]}
    foreach pair $curve {lassign $pair co si; lappend points [expr {-$si}] [expr {$height+$co}]}
    foreach pair $curve {lassign $pair co si; lappend points [expr {-$co}] [expr {-$si}]}
    $canvas create polygon $points -fill #000000 -outline {} -tags bg
    $canvas lower bg
}
proc ::music::show_overlay {} {
    variable overlay_allowed; variable authentication_active
    if {!$overlay_allowed || $authentication_active} {return}
    variable position; variable layout_key; variable overlay_visible
    if {[exists .search]} {close_search}
    if {![exists .overlay]} {
        card .overlay {MusicApp · Player}
        wm attributes .overlay -alpha 0.7 -transparentcolor [c transparent]
        .overlay configure -bg [c transparent]
        canvas .overlay.c -bg [c transparent] -highlightthickness 0
        pack .overlay.c -fill both -expand 1
        foreach {tag font fill} {title main text duration time text_dark lyric lyrics text hint hint text_dark} {
            .overlay.c create text 0 0 -font ::music::font_$font -fill [c $fill] -anchor n -justify center -tags $tag
        }
        .overlay.c itemconfigure hint -text {Both Alt keys · Controls} -state hidden
        drag_handle .overlay.c .overlay 3
        bind .overlay.c <ButtonRelease-3> {::music::overlay_drag_end}
        set layout_key {}
    }
    draw_overlay
    if {$position eq ""} {
        update idletasks
        set position [list [expr {([winfo screenwidth .overlay]-[winfo width .overlay])/2}] 20]
    }
    emit overlay_style [winfo id .overlay] 1
    reveal .overlay; set overlay_visible 1
    place_overlay {*}$position
}
proc ::music::overlay_drag_end {} {
    variable overlay_dragging; set overlay_dragging 0
    variable drag_pulse_timer; variable hint_hide_timer
    if {$drag_pulse_timer ne ""} {after cancel $drag_pulse_timer; set drag_pulse_timer {}}
    if {$hint_hide_timer ne ""} {after cancel $hint_hide_timer}
    set hint_hide_timer [after 10000 {::music::hint_fade 0.0}]
    variable position; variable native_mouse_hooks
    if {!$native_mouse_hooks} {set position [list [winfo x .overlay] [winfo y .overlay]]}
    emit overlay_position {*}$position
}
proc ::music::hide_overlay {} {
    variable overlay_visible; set overlay_visible 0
    if {[exists .overlay]} {wm withdraw .overlay}
    variable overlay_dragging; set overlay_dragging 0
    foreach name {drag_pulse_timer hint_fade_timer hint_hide_timer} {
        variable $name
        if {[set $name] ne ""} {after cancel [set $name]; set $name {}}
    }
    variable hint_opacity; set hint_opacity 0.0
    if {[exists .overlay.c]} {.overlay.c itemconfigure hint -state hidden}
}
proc ::music::overlay_available {allowed} {
    variable overlay_allowed
    if {$overlay_allowed == $allowed} {return}
    set overlay_allowed $allowed
    if {!$allowed} {hide_overlay} elseif {![exists .search]} {show_overlay}
}
proc ::music::overlay_drag_begin {} {
    variable overlay_dragging; variable hint_hide_timer
    set overlay_dragging 1
    if {$hint_hide_timer ne ""} {after cancel $hint_hide_timer; set hint_hide_timer {}}
    hint_fade 1.0
    drag_pulse
}
proc ::music::drag_pulse {} {
    variable overlay_dragging; variable drag_pulse_timer
    if {$drag_pulse_timer ne ""} {after cancel $drag_pulse_timer; set drag_pulse_timer {}}
    if {$overlay_dragging && [exists .overlay]} {
        # Wake the blocking native event loop only during a drag, not at idle.
        set drag_pulse_timer [after 8 ::music::drag_pulse]
    }
}
proc ::music::hint_fade {target} {
    variable hint_opacity; variable hint_fade_timer
    if {$hint_fade_timer ne ""} {after cancel $hint_fade_timer; set hint_fade_timer {}}
    if {![exists .overlay.c]} {return}
    set step [expr {$target > $hint_opacity ? 0.1 : -0.1}]
    set hint_opacity [expr {max(0.0,min(1.0,$hint_opacity+$step))}]
    if {abs($hint_opacity-$target)<0.001} {set hint_opacity $target}
    set shade [expr {int(170*$hint_opacity)}]
    .overlay.c itemconfigure hint -fill [format #%02x%02x%02x $shade $shade $shade] -state [expr {$hint_opacity>0 ? "normal" : "hidden"}]
    if {$hint_opacity != $target} {set hint_fade_timer [after 16 [list ::music::hint_fade $target]]}
}
proc ::music::reveal {w} {
    variable authentication_active; variable authentication_windows
    if {$authentication_active} {
        if {[lsearch -exact $authentication_windows $w]<0} {lappend authentication_windows $w}
        wm withdraw $w
        return
    }
    wm deiconify $w
}
proc ::music::authentication_ui {active} {
    variable authentication_active; variable authentication_windows; variable overlay_allowed
    if {$active} {
        if {$authentication_active} {return}
        set authentication_windows {}
        set authentication_active 1
        foreach w [winfo children .] {
            if {[winfo toplevel $w] eq $w && [wm state $w] ne "withdrawn"} {
                lappend authentication_windows $w
                catch {grab release $w}
                wm withdraw $w
            }
        }
        update idletasks
    } else {
        set authentication_active 0
        set windows $authentication_windows; set authentication_windows {}
        foreach w $windows {
            if {[exists $w] && ($w ne ".overlay" || $overlay_allowed)} {reveal $w}
        }
    }
}
proc ::music::set_position {x y} {
    variable position; set position [list $x $y]
    if {[exists .overlay]} {place_overlay $x $y}
}
proc ::music::place_overlay {x y {width {}} {height {}}} {
    variable position; set position [list $x $y]
    if {![exists .overlay]} {return}
    # Tk interprets a negative geometry offset relative to the screen's far edge.
    # Apply size through Tk, then absolute virtual-desktop coordinates through
    # Win32. Flush pending geometry first so a later map/resize cannot undo it.
    if {$width ne "" && $height ne ""} {wm geometry .overlay [expr {int($width)}]x[expr {int($height)}]}
    if {$width ne "" && $height ne ""} {update idletasks}
    emit overlay_place [winfo id .overlay] $x $y
}
proc ::music::time_text {seconds} {return [format %d:%02d [expr {int($seconds)/60}] [expr {int($seconds)%60}]]}
proc ::music::set_display {new_title elapsed total new_lyrics} {
    variable title; variable duration; variable lyric
    set next_duration "[time_text $elapsed] / [time_text $total]"
    if {$title eq $new_title && $duration eq $next_duration && $lyric eq $new_lyrics} {return}
    set title $new_title; set duration $next_duration; set lyric $new_lyrics
    draw_overlay
}
proc ::music::draw_overlay {} {
    variable title; variable duration; variable lyric; variable lyrics_visible; variable layout_key; variable position
    if {![exists .overlay.c]} {return}
    set ly [expr {$lyrics_visible ? $lyric : ""}]
    set key [list $title $ly [string length $duration]]
    if {$key eq $layout_key} {.overlay.c itemconfigure duration -text $duration; return}
    set layout_key $key; set text [wrap $title 35]; set ly [wrap $ly 40]
    set width [font measure ::music::font_time $duration]
    foreach {value name} [list $text main $ly lyrics {Both Alt keys · Controls} hint] {
        foreach line [split $value \n] {set width [expr {max($width,[font measure ::music::font_$name $line])}]}
    }
    incr width 30
    set mainheight [expr {[font metrics ::music::font_main -linespace]*max(1,[llength [split $text \n]])}]
    set timeheight [font metrics ::music::font_time -linespace]
    set hintheight [font metrics ::music::font_hint -linespace]
    set lyheight [expr {$ly ne "" ? [font metrics ::music::font_lyrics -linespace]*[llength [split $ly \n]]+7.5 : 0}]
    set height [expr {$mainheight+$timeheight+$lyheight+$hintheight+45}]
    .overlay.c delete bg; rounded .overlay.c $width $height 15
    .overlay.c itemconfigure title -text $text; .overlay.c coords title [expr {$width/2.0}] 15
    .overlay.c itemconfigure duration -text $duration; .overlay.c coords duration [expr {$width/2.0}] [expr {15+$mainheight+7.5}]
    if {$ly ne ""} {
        .overlay.c itemconfigure lyric -text $ly -state normal
        .overlay.c coords lyric [expr {$width/2.0}] [expr {15+$mainheight+7.5+$timeheight+7.5}]
    } else {.overlay.c itemconfigure lyric -state hidden}
    .overlay.c coords hint [expr {$width/2.0}] [expr {$height-15-$hintheight}]
    if {$position eq ""} {wm geometry .overlay ${width}x[expr {int($height)}]} else {place_overlay {*}$position $width $height}
}

proc ::music::shortcut {id} {
    variable chords; variable alt_required
    set chord [dict get $chords $id]
    if {$id eq "show_hints"} {return {Both Alt keys}}
    set names [dict create alt {Left Alt} {right alt} {Right Alt} shift {Left Shift} {right shift} {Right Shift} ctrl Ctrl escape Esc space Space enter Enter backspace Backspace delete Delete up ↑ down ↓ left ← right →]
    set parts {}
    foreach key $chord {
        if {$key eq "alt" && !$alt_required($id)} {continue}
        lappend parts [expr {[dict exists $names $key] ? [dict get $names $key] : [string toupper $key]}]
    }
    return [join $parts { + }]
}
proc ::music::controls {{customize 0}} {
    variable labels; variable groups; variable status; variable controls_tab
    if {[exists .controls]} {destroy .controls; return}
    card .controls {MusicApp · Controls}
    frame .controls.body -bg [c window_bg] -padx 22 -pady 18; pack .controls.body -fill both -expand 1
    set body .controls.body
    label $body.heading -text {Music, at your fingertips} -bg [c window_bg] -fg [c text] -font {{Segoe UI} 20 bold} -anchor w
    pack $body.heading -fill x; drag_handle $body.heading .controls
    label $body.help -text "[shortcut show_hints] opens this card from anywhere.  Esc closes it." -bg [c window_bg] -fg [c text_dark] -font {{Segoe UI} 10} -anchor w
    pack $body.help -fill x -pady {6 14}
    frame $body.tabs -bg [c window_bg]; pack $body.tabs -fill x -pady {0 14}
    frame $body.content -bg [c window_bg]; pack $body.content -fill both -expand 1
    foreach page {quick shortcuts} {frame $body.content.$page -bg [c window_bg]}
    set status {}
    label $body.status -textvariable ::music::status -bg [c window_bg] -fg [c warning] -font {{Segoe UI} 10} -anchor w -justify left -wraplength 600
    pack $body.status -fill x -pady {10 0}
    frame $body.footer -bg [c window_bg]; pack $body.footer -fill x -pady {12 0}
    button $body.footer.done -text Done -command {destroy .controls} -bg [c accent] -fg [c window_bg] -activebackground [c accent_active] -activeforeground [c window_bg] -relief flat -bd 0 -font {{Segoe UI} 11} -padx 18 -pady 7 -cursor hand2
    pack $body.footer.done -side right -padx {12 0}
    label $body.footer.hint -text "Media keys work too. Right-drag to move the player.\nIn games, borderless fullscreen keeps the player visible." -bg [c window_bg] -fg [c text_dark] -font {{Segoe UI} 9} -anchor w -justify left -wraplength 480
    pack $body.footer.hint -side left -fill x -expand 1
    bind $body.footer <Configure> {::music::controls_footer %w}
    set page $body.content.quick; set n 0
    foreach group [lrange $groups 0 2] {
        lassign $group heading ids
        label $page.h$n -text [string toupper $heading] -bg [c window_bg] -fg [c text_dark] -font {{Segoe UI} 9 bold} -anchor w
        pack $page.h$n -fill x -pady [list [expr {$n ? 12 : 0}] 6]
        frame $page.g$n -bg [c window_bg]; pack $page.g$n -fill x
        for {set i 0} {$i<3} {incr i} {grid columnconfigure $page.g$n $i -weight 1 -uniform controls}
        set i 0
        foreach id $ids {
            button $page.g$n.$id -text "[dict get $labels $id]\n[shortcut $id]" -command [list ::music::control_action $id] -bg [c frame_bg] -fg [c text] -activebackground [c entry_bg] -activeforeground [c accent] -relief flat -bd 0 -font {{Segoe UI} 11} -padx 6 -pady 9 -cursor hand2 -takefocus 1
            grid $page.g$n.$id -row [expr {$i/3}] -column [expr {$i%3}] -sticky ew -padx 3 -pady 3
            incr i
        }
        incr n
    }
    foreach {index text} {0 {Quick controls} 1 Shortcuts} {
        button $body.tabs.t$index -text $text -command [list ::music::select_tab $index] -relief flat -bd 0 -font {{Segoe UI} 11} -padx 16 -pady 8 -activebackground [c accent_active] -cursor hand2
        pack $body.tabs.t$index -side left -padx {0 6}
    }
    select_tab $customize
    bind .controls <Escape> ::music::controls_escape
    update idletasks
    set width [expr {min(max(380,[winfo screenwidth .controls]-40),max(660,[winfo reqwidth .controls]))}]
    set height [expr {min(max(400,[winfo screenheight .controls]-100),max(610,[winfo reqheight .controls]))}]
    center .controls $width $height; wm minsize .controls [expr {min(580,$width)}] [expr {min(480,$height)}]
    reveal .controls
}
proc ::music::controls_footer {width} {
    if {[exists .controls.body.footer.hint]} {.controls.body.footer.hint configure -wraplength [expr {max(160,$width-[winfo reqwidth .controls.body.footer.done]-12)}]}
}
proc ::music::control_action {id} {
    if {$id in {show_search show_eq_menu youtube_sync player_on_off}} {destroy .controls}
    emit $id
}
proc ::music::select_tab {index} {
    variable controls_tab; set controls_tab $index
    if {$index} {build_editor}
    foreach p {quick shortcuts} {pack forget .controls.body.content.$p}
    pack .controls.body.content.[expr {$index ? "shortcuts" : "quick"}] -fill both -expand 1
    foreach i {0 1} {.controls.body.tabs.t$i configure -bg [c [expr {$i==$index ? "accent" : "frame_bg"}]] -fg [c [expr {$i==$index ? "window_bg" : "text_dark"}]]}
}
proc ::music::build_editor {} {
    variable groups; variable labels; variable chords; variable alt_required
    set page .controls.body.content.shortcuts
    if {[exists $page.scroll]} {return}
    label $page.help -text {Choose Change, then press a key. Your Alt side stays the same.} -bg [c window_bg] -fg [c text_dark] -font {{Segoe UI} 10} -anchor w
    pack $page.help -fill x -pady {0 10}
    frame $page.scroll -bg [c window_bg]; pack $page.scroll -fill both -expand 1
    canvas $page.scroll.c -bg [c window_bg] -highlightthickness 0 -yscrollcommand [list $page.scroll.s set]
    ttk::scrollbar $page.scroll.s -orient vertical -command [list $page.scroll.c yview]
    frame $page.scroll.c.rows -bg [c window_bg]
    $page.scroll.c create window 0 0 -window $page.scroll.c.rows -anchor nw -tags rows
    bind $page.scroll.c.rows <Configure> [list ::music::scrollregion $page.scroll.c]
    bind $page.scroll.c <Configure> [list $page.scroll.c itemconfigure rows -width %w]
    bind .controls <MouseWheel> [list ::music::wheel $page.scroll.c %D]
    pack $page.scroll.c -side left -fill both -expand 1; pack $page.scroll.s -side right -fill y
    set rows $page.scroll.c.rows; set n 0
    foreach group $groups {
        lassign $group heading ids
        label $rows.h$n -text [string toupper $heading] -bg [c window_bg] -fg [c text_dark] -font {{Segoe UI} 9 bold} -anchor w
        pack $rows.h$n -fill x -pady {12 6}; incr n
        foreach id $ids {
            set row $rows.$id
            frame $row -bg [c frame_bg] -padx 10 -pady 8; pack $row -fill x -pady 2
            grid columnconfigure $row 0 -weight 1
            label $row.name -text [dict get $labels $id] -bg [c frame_bg] -fg [c text] -font {{Segoe UI} 11} -anchor w
            grid $row.name -row 0 -column 0 -sticky w
            label $row.chord -text [shortcut $id] -bg [c frame_bg] -fg [c accent] -font {{Segoe UI} 10} -anchor w
            grid $row.chord -row 1 -column 0 -sticky w -pady {2 0}
            if {$id ni {player_on_off show_hints player_restart kill_self}} {
                if {"alt" in [dict get $chords $id]} {
                    checkbutton $row.alt -text {Require Left Alt} -variable ::music::alt_required($id) -command [list ::music::emit binding_alt $id] -bg [c frame_bg] -fg [c text_dark] -activebackground [c frame_bg] -activeforeground [c text] -selectcolor [c entry_bg] -font {{Segoe UI} 10} -bd 0
                    grid $row.alt -row 0 -column 1 -rowspan 2 -padx 6
                }
                button $row.change -text Change -command [list ::music::edit_binding $id] -bg [c entry_bg] -fg [c text] -activebackground [c button_active] -activeforeground [c text] -relief flat -bd 0 -font {{Segoe UI} 10} -padx 12 -pady 6 -cursor hand2
                grid $row.change -row 0 -column 2 -rowspan 2
            } else {
                label $row.fixed -text Fixed -bg [c frame_bg] -fg [c text_dark] -font {{Segoe UI} 10}
                grid $row.fixed -row 0 -column 2 -rowspan 2 -padx 12
            }
        }
    }
    button $page.reset -text {Restore default shortcuts} -command ::music::reset_bindings -bg [c window_bg] -fg [c text_dark] -activebackground [c frame_bg] -activeforeground [c text] -relief flat -bd 0 -font {{Segoe UI} 10} -pady 8 -cursor hand2
    pack $page.reset -anchor w -pady {8 0}
}
proc ::music::scrollregion {canvas} {if {[exists $canvas]} {$canvas configure -scrollregion [$canvas bbox all]}}
proc ::music::wheel {canvas delta} {if {[exists $canvas]} {$canvas yview scroll [expr {-int($delta/120)}] units}}
proc ::music::edit_binding {id} {
    variable edit_action; variable status; variable labels; variable chords
    set edit_action $id
    set modifier [expr {{right alt} in [dict get $chords $id] ? "Right Alt" : "Left Alt"}]
    set status "Changing [dict get $labels $id]: press a key for $modifier + key. Esc cancels."
    emit binding_edit $id
}
proc ::music::controls_escape {} {
    variable edit_action; variable status
    if {$edit_action ne ""} {set edit_action {}; set status {}; emit binding_cancel} else {destroy .controls}
    return -code break
}
proc ::music::reset_bindings {} {
    if {[tk_messageBox -parent .controls -type yesno -icon question -title {Restore shortcuts} -message {Restore the original shortcuts? Your custom shortcuts will be replaced.}] eq "yes"} {emit binding_reset}
}
proc ::music::binding_update {id chord require_alt {message {}}} {
    variable chords; variable alt_required; variable status; variable edit_action
    dict set chords $id $chord; set alt_required($id) $require_alt; set status $message; set edit_action {}
    if {[exists .controls]} {
        set geometry [wm geometry .controls]; variable controls_tab; set tab $controls_tab
        destroy .controls; controls $tab; wm geometry .controls $geometry; set status $message
    }
}

proc ::music::search {} {
    variable restore_overlay; variable overlay_visible; variable search_term; variable searching; variable downloading
    if {$searching || $downloading} {return}
    if {[exists .search]} {close_search; return}
    if {[exists .controls]} {destroy .controls}
    set restore_overlay $overlay_visible
    if {$restore_overlay} {hide_overlay}
    set search_term {}
    card .search {MusicApp · Search}
    ttk::frame .search.main -style Accent.TFrame -padding 20; pack .search.main -fill both -expand 1
    grid columnconfigure .search.main 0 -weight 1; grid rowconfigure .search.main 1 -weight 1
    ttk::frame .search.main.top -style Accent.TFrame
    grid .search.main.top -row 0 -column 0 -sticky ew -pady {0 15}; grid columnconfigure .search.main.top 0 -weight 1
    ttk::entry .search.main.top.entry -textvariable ::music::search_term -font {{Segoe UI} 13} -style TEntry
    grid .search.main.top.entry -row 0 -column 0 -sticky ew -ipady 5
    ttk::frame .search.main.top.checks -style Accent.TFrame
    grid .search.main.top.checks -row 0 -column 1 -sticky e -padx {10 0}
    ttk::checkbutton .search.main.top.checks.youtube -text YouTube -variable ::music::use_youtube -command ::music::search_youtube
    pack .search.main.top.checks.youtube -side left
    ttk::checkbutton .search.main.top.checks.download -text Download -variable ::music::permanent -command {::music::emit download_permanent $::music::permanent}
    pack .search.main.top.checks.download -side left -padx {5 0}
    ttk::frame .search.main.results -style Accent.TFrame
    grid .search.main.results -row 1 -column 0 -sticky nsew
    grid rowconfigure .search.main.results 0 -weight 1; grid columnconfigure .search.main.results 0 -weight 1
    listbox .search.main.results.list -font {{Segoe UI} 12} -bg [c entry_bg] -fg [c text] -selectmode single -activestyle none -selectbackground [c accent] -selectforeground [c frame_bg] -relief flat -bd 0 -highlightthickness 0 -yscrollcommand {.search.main.results.scroll set}
    ttk::scrollbar .search.main.results.scroll -orient vertical -command {.search.main.results.list yview}
    grid .search.main.results.scroll -row 0 -column 1 -sticky ns; grid .search.main.results.list -row 0 -column 0 -sticky nsew
    ttk::label .search.main.results.spinner -text {} -style Status.TLabel -font {Consolas 12} -background [c entry_bg]
    canvas .search.main.record -bg [c window_bg] -highlightthickness 0
    bind .search.main.top.entry <KeyRelease> ::music::search_debounce
    bind .search.main.top.entry <Return> {::music::select_result; break}
    bind .search.main.top.entry <Down> {::music::search_nav 1; break}
    bind .search.main.top.entry <Up> {::music::search_nav -1; break}
    bind .search.main.results.list <Double-Button-1> ::music::select_result
    bind .search.main.results.list <Return> {::music::select_result; break}
    bind .search <Escape> {::music::close_search; break}
    drag_handle .search.main .search; drag_handle .search.main.top .search
    center .search 500 400; reveal .search; raise .search; grab .search; focus -force .search.main.top.entry
    emit search_recommendation
}
proc ::music::search_debounce {} {
    variable search_timer
    if {$search_timer ne ""} {after cancel $search_timer}
    set search_timer [after 1000 ::music::search_submit]
}
proc ::music::search_youtube {} {variable use_youtube; emit search_youtube $use_youtube; search_submit}
proc ::music::search_submit {{term {}} {dont_log 0}} {
    variable search_term; variable use_youtube; variable searching
    if {$term eq ""} {set term [string trim $search_term]}
    if {$searching || $term eq "" || ![exists .search]} {return}
    set searching 1; grid remove .search.main.results.list; grid remove .search.main.results.scroll
    place .search.main.results.spinner -relx .5 -rely .5 -anchor center
    search_spinner 0; emit search $term $use_youtube $dont_log
}
proc ::music::search_spinner {index} {
    variable searching; variable spinner_timer
    if {$searching && [exists .search.main.results.spinner]} {
        .search.main.results.spinner configure -text "Searching [lindex [list | / - \\] [expr {$index%4}]]"
        set spinner_timer [after 150 [list ::music::search_spinner [incr index]]]
    }
}
proc ::music::results {rows} {
    variable searching; variable search_results; variable spinner_timer; variable use_youtube
    set searching 0; set search_results $rows
    if {$spinner_timer ne ""} {after cancel $spinner_timer; set spinner_timer {}}
    if {![exists .search]} {return}
    place forget .search.main.results.spinner
    grid .search.main.results.list; grid .search.main.results.scroll
    set list .search.main.results.list; $list delete 0 end
    foreach row $rows {
        lassign $row title path type
        if {$type eq "title_only"} {
            set spaces [expr {max(0,[winfo width $list]/[font measure {{Segoe UI} 12} 0]-[string length $title]/2-5)}]
            $list insert end "[string repeat { } $spaces]$title"
        } elseif {$type eq "playlist"} {$list insert end " \[Playlist\] $title"} else {
            set tag [expr {!$use_youtube ? "" : ($type eq "url" ? "\[YouTube\]" : "\[Local\]")}]
            $list insert end " $tag $title"
        }
    }
    if {![llength $rows]} {$list insert end {  No results found.}; $list itemconfigure 0 -fg [c placeholder]}
}
proc ::music::search_nav {delta} {
    set w .search.main.results.list; set size [$w size]
    if {!$size} {return}
    set selection [$w curselection]; set index 0
    if {[llength $selection]} {set index [expr {max(0,min($size-1,[lindex $selection 0]+$delta))}]}
    $w selection clear 0 end; $w selection set $index; $w activate $index; $w see $index
}
proc ::music::select_result {} {
    variable search_results; variable downloading
    set selection [.search.main.results.list curselection]
    if {![llength $selection] || ![llength $search_results]} {search_submit; return}
    lassign [lindex $search_results [lindex $selection 0]] title path type
    if {$type eq "title_only"} {return}
    set downloading 1
    if {$type eq "url"} {download_animation}
    emit search_select $path $type
}
proc ::music::download_animation {} {
    grid remove .search.main.top; grid remove .search.main.results
    wm attributes .search -alpha .2 -transparentcolor [c window_bg]
    grid .search.main.record -row 0 -column 0 -rowspan 2 -sticky nsew
    emit search_style [winfo id .search] 1
    record_frame 0
}
proc ::music::record_frame {angle} {
    variable record_timer; variable downloading
    if {!$downloading || ![exists .search.main.record]} {return}
    set c .search.main.record; $c delete all
    set cx [expr {[winfo width $c]/2}]; set cy [expr {[winfo height $c]/2}]
    if {$cx>5 && $cy>5} {
        set outer [expr {min($cx,$cy)*.7}]; set inner [expr {min($cx,$cy)*.3}]; set label [expr {min($cx,$cy)*.25}]
        $c create oval [expr {$cx-$outer}] [expr {$cy-$outer}] [expr {$cx+$outer}] [expr {$cy+$outer}] -fill #111 -outline [c accent] -width 2
        for {set i 0} {$i<12} {incr i} {set r [expr {$inner+($outer-$inner)*($i/12.0)}]; $c create oval [expr {$cx-$r}] [expr {$cy-$r}] [expr {$cx+$r}] [expr {$cy+$r}] -outline #333}
        foreach offset {45 135} {
            set a [expr {($angle+$offset)*acos(-1)/180}]
            $c create line [expr {$cx+$inner*cos($a)}] [expr {$cy+$inner*sin($a)}] [expr {$cx+$outer*cos($a)}] [expr {$cy+$outer*sin($a)}] -fill [c accent] -width 3
        }
        $c create oval [expr {$cx-$label}] [expr {$cy-$label}] [expr {$cx+$label}] [expr {$cy+$label}] -fill [c accent_active] -outline {}
        $c create text $cx $cy -text ♪ -font [list {Segoe UI Symbol} [expr {max(1,int($label*1.2))}]] -fill #111
    }
    set record_timer [after 150 [list ::music::record_frame [expr {$angle+15}]]]
}
proc ::music::close_search {} {
    variable search_timer; variable spinner_timer; variable record_timer; variable searching; variable downloading; variable restore_overlay
    foreach name {search_timer spinner_timer record_timer} {if {[set $name] ne ""} {after cancel [set $name]; set $name {}}}
    set searching 0; set downloading 0
    if {[exists .search]} {catch {grab release .search}; destroy .search}
    set restore_overlay 0
    # Readiness may have changed while search was open. show_overlay checks
    # the current playback/user/authentication gate before revealing anything.
    show_overlay
}

proc ::music::knob_create {w radius value maximum kind action} {
    variable knob
    set size [expr {$radius*2+4}]
    canvas $w -width $size -height $size -bg [c window_bg] -highlightthickness 0
    set value [expr {max(-$maximum,min($maximum,$value))}]
    set knob($w) [dict create radius $radius value $value max $maximum kind $kind action $action last 0]
    $w create oval 2 2 [expr {2+$radius*2}] [expr {2+$radius*2}] -fill #222 -outline #555 -width 2 -tags ring
    $w create line 0 0 0 0 -fill #2ee -width 3 -capstyle round -tags pointer
    $w create text [expr {$radius+2}] [expr {$radius+2}] -fill #ddd -font {{Segoe UI} 8 bold} -tags value
    bind $w <Button-1> [list ::music::knob_drag $w %x %y 0]
    bind $w <B1-Motion> [list ::music::knob_drag $w %x %y 0]
    bind $w <ButtonRelease-1> [list ::music::knob_drag $w %x %y 1]
    knob_draw $w
}
proc ::music::knob_draw {w} {
    variable knob
    if {![exists $w] || ![info exists knob($w)]} {return}
    set state $knob($w); set r [dict get $state radius]; set value [dict get $state value]
    set a [expr {double($value)/[dict get $state max]*150*acos(-1)/180}]
    $w coords pointer [expr {$r+2}] [expr {$r+2}] [expr {$r+2+$r*.75*sin($a)}] [expr {$r+2-$r*.75*cos($a)}]
    set suffix [expr {[dict get $state kind] eq "eq" ? " dB" : "%"}]
    $w itemconfigure value -text "[format %+.1f $value]$suffix"
}
proc ::music::knob_drag {w x y commit} {
    variable knob; variable preset
    if {[$w cget -state] eq "disabled"} {return}
    set r [dict get $knob($w) radius]
    set angle [expr {atan2($x-($r+2),($r+2)-$y)*180/acos(-1)}]
    set value [expr {round(max(-150,min(150,$angle))/150.0*[dict get $knob($w) max]*10)/10.0}]
    dict set knob($w) value $value; knob_draw $w
    set now [clock milliseconds]
    if {$commit || $now-[dict get $knob($w) last]>=10} {
        dict set knob($w) last $now
        set action [dict get $knob($w) action]
        if {[dict get $knob($w) kind] eq "eq"} {set preset Custom}
        {*}$action $value
    }
}
proc ::music::echo_delay {value} {variable delay; set delay [expr {max(1,int($value))}]; emit echo_delay [expr {max(10,int($value*10))}]}
proc ::music::echo_wet {value} {variable wet; set wet [expr {max(0,int($value))}]; emit echo_wet $wet}
proc ::music::volume_drag {x} {
    variable volume
    set volume [expr {round((max(10,min(110,$x))-10)/100.0*100)}]
    volume_draw; emit volume $volume
}
proc ::music::volume_draw {} {
    variable volume
    set w .eq.card.echo.volume
    if {![exists $w]} {return}
    set position [expr {10+$volume}]
    $w coords thumb [expr {$position-10}] 2 [expr {$position+10}] 22
    $w itemconfigure value -text "[format %.0f $volume]%"
}
proc ::music::apply_preset {} {
    variable preset; variable presets
    if {$preset eq "Custom" || ![dict exists $presets $preset]} {return}
    set values [dict get $presets $preset]; variable knob
    set i 0
    foreach freq {31 62 125 250 500 1000 2000 4000 8000 16000} {
        set w .eq.card.grid.f$freq.k
        if {[info exists knob($w)]} {dict set knob($w) value [lindex $values $i]; knob_draw $w}
        incr i
    }
    emit eq_preset $preset {*}$values
}
proc ::music::eq {} {
    variable presets; variable preset; variable gaming; variable volume; variable delay; variable wet
    if {[exists .eq]} {destroy .eq; return}
    card .eq {MusicApp · Sound & effects}
    set maxcols [expr {max(1,([winfo screenwidth .]-100)/64)}]
    set rows [expr {int(ceil(10.0/$maxcols))}]
    set width [expr {min(10,$maxcols)*64+40}]; set height [expr {$rows*110+190}]
    canvas .eq.card -width $width -height $height -bg [c window_bg] -highlightthickness 0
    pack .eq.card -fill both -expand 1
    .eq.card create rectangle 0 0 $width $height -fill [c window_bg] -outline {}
    ttk::frame .eq.card.grid -style Accent.TFrame
    place .eq.card.grid -relx .5 -rely .05 -anchor n
    set i 0
    foreach freq {31 62 125 250 500 1000 2000 4000 8000 16000} {
        set col .eq.card.grid.f$freq; ttk::frame $col -style Accent.TFrame
        grid $col -row [expr {$i/$maxcols}] -column [expr {$i%$maxcols}] -padx 6 -pady 2
        ttk::label $col.label -background [c window_bg] -text [expr {$freq>=1000 ? "[expr {$freq/1000}]k" : $freq}]
        pack $col.label
        variable bands
        set value [expr {[info exists bands($freq)] ? $bands($freq) : 0}]
        knob_create $col.k 26 $value 12 eq [list ::music::emit eq_band $freq]; pack $col.k
        incr i
    }
    ttk::menubutton .eq.card.preset -textvariable ::music::preset -menu .eq.card.preset.menu -style TMenubutton
    menu .eq.card.preset.menu -tearoff 0 -bg [c entry_bg] -fg [c text] -activebackground [c button] -activeforeground [c accent] -relief flat
    foreach name [concat [dict keys $presets] Custom] {.eq.card.preset.menu add radiobutton -label $name -value $name -variable ::music::preset -command ::music::apply_preset}
    .eq.card create window [expr {$width/2}] [expr {int($height*.48)}] -window .eq.card.preset -anchor n
    ttk::frame .eq.card.echo -style Accent.TFrame; place .eq.card.echo -relx .5 -rely .63 -anchor n
    ttk::label .eq.card.echo.heading -background [c window_bg] -text Echo -font {{Segoe UI} 9 bold}
    grid .eq.card.echo.heading -row 0 -column 0 -columnspan 2 -pady {0 3}
    foreach {id label column value action} [list delay {Delay ×10 ms} 0 $delay ::music::echo_delay wet {Wet %} 1 $wet ::music::echo_wet] {
        ttk::label .eq.card.echo.${id}label -background [c window_bg] -text $label
        grid .eq.card.echo.${id}label -row 1 -column $column -padx 6 -pady 2
        knob_create .eq.card.echo.$id 20 $value 100 percent $action
        grid .eq.card.echo.$id -row 2 -column $column -padx 6
    }
    ttk::label .eq.card.echo.volumelabel -background [c window_bg] -text {Volume %}
    grid .eq.card.echo.volumelabel -row 1 -column 2 -padx 6 -pady 2
    canvas .eq.card.echo.volume -width 120 -height 24 -bg [c window_bg] -highlightthickness 0
    grid .eq.card.echo.volume -row 2 -column 2 -padx 6
    .eq.card.echo.volume create line 10 12 110 12 -fill #555 -width 6 -capstyle round
    .eq.card.echo.volume create oval 0 0 0 0 -fill #2ee -outline {} -tags thumb
    .eq.card.echo.volume create text 60 12 -fill #ddd -font {{Segoe UI} 8 bold} -tags value
    bind .eq.card.echo.volume <Button-1> {::music::volume_drag %x}
    bind .eq.card.echo.volume <B1-Motion> {::music::volume_drag %x}
    bind .eq.card.echo.volume <ButtonRelease-1> {::music::volume_drag %x}
    volume_draw
    ttk::frame .eq.card.gaming -style Accent.TFrame; place .eq.card.gaming -relx .1 -rely .88 -anchor n
    ttk::checkbutton .eq.card.gaming.check -text {Gaming Mode} -variable ::music::gaming -command {::music::gaming_update; ::music::emit gaming $::music::gaming}
    pack .eq.card.gaming.check -padx 10 -pady {5 0}
    ttk::frame .eq.card.sync -style Accent.TFrame; place .eq.card.sync -relx .1 -rely .78 -anchor n
    ttk::checkbutton .eq.card.sync.check -text {Play Radio EQ} -variable ::music::accept_radio_eq -command {::music::emit accept_radio_eq $::music::accept_radio_eq}
    pack .eq.card.sync.check -padx 10 -pady {5 0}
    drag_handle .eq.card .eq 3
    bind .eq <Escape> {destroy .eq; break}
    bind .eq <FocusOut> {after idle ::music::eq_focus_out}
    center .eq $width $height; reveal .eq; gaming_update
    emit eq_request
}
proc ::music::eq_focus_out {} {if {[exists .eq] && [focus -displayof .eq] eq ""} {destroy .eq}}
proc ::music::gaming_update {} {
    variable gaming; variable knob
    if {![exists .eq]} {return}
    foreach w [array names knob] {if {[exists $w]} {$w configure -state [expr {$gaming ? "disabled" : "normal"}]; $w itemconfigure ring -fill [expr {$gaming ? "#555" : "#222"}]}}
    .eq.card.preset configure -state [expr {$gaming ? "disabled" : "normal"}]
}
proc ::music::eq_state {gains volume_value delay_ms wet_pct gaming_mode radio_eq} {
    variable bands; variable volume; variable delay; variable wet; variable gaming; variable accept_radio_eq; variable knob; variable presets; variable preset
    set volume $volume_value; set delay [expr {$delay_ms/10.0}]; set wet $wet_pct; set gaming $gaming_mode; set accept_radio_eq $radio_eq
    set current {}; set i 0
    foreach freq {31 62 125 250 500 1000 2000 4000 8000 16000} {
        set bands($freq) [lindex $gains $i]; lappend current $bands($freq)
        set w .eq.card.grid.f$freq.k
        if {[info exists knob($w)]} {dict set knob($w) value $bands($freq); knob_draw $w}
        incr i
    }
    set preset Custom
    dict for {name values} $presets {
        set matches 1
        foreach a $values b $current {if {$a!=$b} {set matches 0; break}}
        if {$matches} {set preset $name}
    }
    foreach id {delay wet} {set w .eq.card.echo.$id; if {[info exists knob($w)]} {dict set knob($w) value [set $id]; knob_draw $w}}
    volume_draw; gaming_update
}

proc ::music::youtube {{startup 0}} {
    variable yt_startup; variable yt_collapsed; variable yt_signature
    variable yt_layout_signature; variable yt_progress_mode; variable yt_progress_max; variable yt_progress_value
    variable yt_download_signature; variable yt_reports; variable yt_frame_visible; variable yt_rows
    set yt_startup $startup
    if {[exists .youtube]} {
        if {[wm state .youtube] ne "withdrawn"} {wm withdraw .youtube} else {youtube_restore; reveal .youtube; raise .youtube}
        return
    }
    set yt_collapsed 0; set yt_signature {}
    set yt_layout_signature {}; set yt_progress_mode {}; set yt_progress_max {}; set yt_progress_value {}
    set yt_download_signature {}; set yt_reports {}; set yt_frame_visible 0; array unset yt_rows
    card .youtube {MusicApp · YouTube}; wm resizable .youtube 0 0; wm geometry .youtube 440x245
    set body .youtube.body
    frame $body -bg [c window_bg] -padx 26 -pady 23; pack $body -fill both -expand 1
    label $body.heading -text {Connect YouTube} -font {{Segoe UI} 18 bold} -fg [c accent] -bg [c window_bg] -anchor w -padx 0
    pack $body.heading -fill x -padx {0 32}; drag_handle $body.heading .youtube
    label $body.message -textvariable ::music::yt_message -font {{Segoe UI} 11} -justify left -wraplength 382 -fg [c text] -bg [c window_bg]
    pack $body.message -fill x -pady {14 20}
    frame $body.buttons -bg [c window_bg]; pack $body.buttons -side bottom -fill x
    ttk::progressbar $body.progress -style MusicApp.Horizontal.TProgressbar -mode indeterminate -maximum 100
    pack $body.progress -fill x -before $body.buttons -pady {0 16}
    frame $body.downloads -bg [c window_bg]
    for {set i 0} {$i<3} {incr i} {
        frame $body.downloads.r$i -bg [c window_bg]
        label $body.downloads.r$i.label -bg [c window_bg] -fg [c text] -font {{Segoe UI} 9} -anchor w -justify left -wraplength 382
        pack $body.downloads.r$i.label -fill x
        ttk::progressbar $body.downloads.r$i.progress -style MusicApp.Horizontal.TProgressbar -maximum 100
        pack $body.downloads.r$i.progress -fill x -pady {4 10}
    }
    button $body.minus -text − -command ::music::youtube_collapse -bg [c window_bg] -fg [c text_dark] -activebackground [c frame_bg] -activeforeground [c accent] -font {{Segoe UI} 16} -relief flat -bd 0 -cursor hand2 -padx 4 -pady 0
    place $body.minus -relx 1 -x 0 -y -8 -anchor ne
    frame .youtube.mini -bg [c window_bg] -padx 16 -pady 12
    label .youtube.mini.summary -textvariable ::music::yt_message -bg [c window_bg] -fg [c text] -font {{Segoe UI} 10} -justify left -anchor w -wraplength 260
    pack .youtube.mini.summary -side left -fill both -expand 1; drag_handle .youtube.mini.summary .youtube
    button .youtube.mini.restore -text ↗ -command ::music::youtube_restore -bg [c window_bg] -fg [c accent] -activebackground [c frame_bg] -relief flat -bd 0 -font {{Segoe UI} 15} -cursor hand2 -padx 8
    pack .youtube.mini.restore -side right
    bind .youtube <Escape> {::music::youtube_collapse; break}
    wm protocol .youtube WM_DELETE_WINDOW ::music::youtube_close
    youtube_render; reveal .youtube
    if {$startup} {emit youtube_start}
}
proc ::music::youtube_state {phase message connected ready busy saved total {remembered 1}} {
    variable yt_phase; variable yt_message; variable yt_connected; variable yt_ready; variable yt_busy; variable yt_saved; variable yt_total
    if {$connected && !$remembered} {append message "\nSign in next time: Windows couldn't remember this login."}
    set yt_phase $phase; if {$yt_message ne $message} {set yt_message $message}; set yt_connected $connected; set yt_ready $ready; set yt_busy $busy; set yt_saved $saved; set yt_total $total
    youtube_render
}
proc ::music::youtube_render {} {
    variable yt_phase; variable yt_connected; variable yt_ready; variable yt_busy; variable yt_startup; variable yt_signature; variable yt_collapsed
    variable yt_message; variable yt_layout_signature
    if {![exists .youtube]} {return}
    youtube_progress
    set signature [list [expr {$yt_phase eq "error"}] $yt_busy $yt_connected $yt_ready $yt_startup]
    if {$signature ne $yt_signature} {
        set yt_signature $signature; set buttons .youtube.body.buttons
        foreach w [winfo children $buttons] {destroy $w}
        if {$yt_phase eq "error"} {
            .youtube.body.heading configure -text [expr {$yt_startup ? "Setup needs attention" : "Couldn't finish"}]
            card_button $buttons.retry [expr {$yt_startup ? "Retry setup" : "Retry"}] {::music::emit youtube_start} 1
            pack $buttons.retry -side left
            if {$yt_busy} {$buttons.retry configure -state disabled}
            card_button $buttons.close [expr {$yt_startup ? "Quit" : "Close"}] [expr {$yt_startup ? "::music::emit kill_self" : "destroy .youtube"}]
            pack $buttons.close -side right
        } elseif {$yt_busy && !$yt_connected} {
            .youtube.body.heading configure -text {Preparing your music}
            card_button $buttons.cancel Cancel {::music::emit youtube_cancel}; pack $buttons.cancel -side right
        } else {
            .youtube.body.heading configure -text [expr {$yt_busy ? "Downloading your music" : ($yt_ready ? "Your music is ready" : "Connect YouTube")}]
            if {$yt_busy} {card_button $buttons.sync {Stop sync} {::music::emit youtube_cancel}} else {card_button $buttons.sync [expr {$yt_connected ? "Sync music" : "Sign in"}] {::music::emit youtube_start} 1}
            pack $buttons.sync -side left
            card_button $buttons.options Options ::music::youtube_options; pack $buttons.options -side left -padx 8
            card_button $buttons.close [expr {$yt_startup && !$yt_ready ? "Minimize" : "Close"}] ::music::youtube_close; pack $buttons.close -side right
        }
    }
    set layout [list $yt_signature $yt_message $yt_collapsed]
    if {$layout ne $yt_layout_signature} {set yt_layout_signature $layout; youtube_size}
}
proc ::music::youtube_size {} {
    variable yt_collapsed
    if {![exists .youtube]} {return}
    update idletasks
    if {$yt_collapsed} {set wanted 330x[expr {max(64,[winfo reqheight .youtube.mini])}]} else {set wanted [expr {max(440,[winfo reqwidth .youtube.body])}]x[expr {max(245,[winfo reqheight .youtube.body])}]}
    regexp {^[0-9]+x[0-9]+} [wm geometry .youtube] actual
    if {$actual ne $wanted} {wm geometry .youtube $wanted}
}
proc ::music::youtube_progress {} {
    variable yt_saved; variable yt_total; variable yt_busy; variable yt_collapsed; variable yt_timer
    variable yt_progress_mode; variable yt_progress_max; variable yt_progress_value
    if {![exists .youtube.body.progress]} {return}
    set p .youtube.body.progress
    if {$yt_total eq ""} {
        if {$yt_progress_mode ne "indeterminate"} {$p configure -mode indeterminate; set yt_progress_mode indeterminate}
    } else {
        if {$yt_progress_mode ne "determinate"} {$p configure -mode determinate; set yt_progress_mode determinate; set yt_progress_value {}}
        set maximum [expr {max(1,$yt_total)}]
        if {$yt_progress_max ne $maximum} {$p configure -maximum $maximum; set yt_progress_max $maximum}
        if {$yt_progress_value ne $yt_saved} {$p configure -value $yt_saved; set yt_progress_value $yt_saved}
    }
    if {$yt_total eq "" && $yt_busy && !$yt_collapsed} {
        if {$yt_timer eq ""} {youtube_progress_tick}
    } elseif {$yt_timer ne ""} {after cancel $yt_timer; set yt_timer {}}
}
proc ::music::youtube_progress_tick {} {
    variable yt_timer; variable yt_total; variable yt_busy; variable yt_collapsed
    set yt_timer {}
    if {[exists .youtube.body.progress] && $yt_total eq "" && $yt_busy && !$yt_collapsed} {
        .youtube.body.progress step 3
        set yt_timer [after 80 ::music::youtube_progress_tick]
    }
}
proc ::music::youtube_downloads {reports} {
    variable yt_collapsed; variable yt_download_signature; variable yt_reports; variable yt_frame_visible; variable yt_rows
    if {![exists .youtube.body.downloads]} {return}
    set reports [lrange $reports 0 2]
    set signature [list $reports $yt_collapsed]
    if {$signature eq $yt_download_signature} {return}
    set yt_download_signature $signature; set yt_reports $reports
    set frame .youtube.body.downloads
    set visible [expr {[llength $reports]>0}]; set layout_changed 0
    if {$visible!=$yt_frame_visible} {
        if {$visible} {pack $frame -fill x -before .youtube.body.buttons} else {pack forget $frame}
        set yt_frame_visible $visible; set layout_changed 1
    }
    for {set i 0} {$i<3} {incr i} {
        set row $frame.r$i
        if {![info exists yt_rows($i)]} {set yt_rows($i) [dict create visible 0 text {} size {} mode {} value {} running 0]}
        set state $yt_rows($i)
        if {$i>=[llength $reports]} {
            if {[dict get $state running]} {$row.progress stop; dict set state running 0}
            if {[dict get $state visible]} {pack forget $row; dict set state visible 0; set layout_changed 1}
            set yt_rows($i) $state; continue
        }
        lassign [lindex $reports $i] text fraction
        if {[dict get $state text] ne $text} {
            $row.label configure -text $text; dict set state text $text
            set size [list [winfo reqwidth $row.label] [winfo reqheight $row.label]]
            if {$size ne [dict get $state size]} {dict set state size $size; set layout_changed 1}
        }
        if {![dict get $state visible]} {pack $row -fill x; dict set state visible 1; set layout_changed 1}
        set mode [expr {$fraction eq "" ? "indeterminate" : "determinate"}]
        set running [expr {$fraction eq "" && !$yt_collapsed}]
        if {[dict get $state running] && !$running} {$row.progress stop; dict set state running 0}
        if {$mode ne [dict get $state mode]} {$row.progress configure -mode $mode; dict set state mode $mode; dict set state value {}}
        if {$fraction ne ""} {
            set value [expr {max(0,min(1,$fraction))*100}]
            if {$value ne [dict get $state value]} {$row.progress configure -value $value; dict set state value $value}
        }
        if {$running && ![dict get $state running]} {$row.progress start 100; dict set state running 1}
        set yt_rows($i) $state
    }
    if {$layout_changed} {youtube_size}
}
proc ::music::youtube_collapse {} {
    variable yt_collapsed; variable yt_geometry; variable yt_reports
    if {![exists .youtube] || $yt_collapsed} {return}
    set yt_geometry [wm geometry .youtube]; set yt_collapsed 1
    pack forget .youtube.body; pack .youtube.mini -fill both -expand 1
    youtube_downloads $yt_reports
    youtube_progress; youtube_size
}
proc ::music::youtube_restore {} {
    variable yt_collapsed; variable yt_geometry; variable yt_reports
    if {![exists .youtube] || !$yt_collapsed} {return}
    set yt_collapsed 0; pack forget .youtube.mini; pack .youtube.body -fill both -expand 1
    if {$yt_geometry ne ""} {wm geometry .youtube $yt_geometry}
    youtube_render; youtube_downloads $yt_reports
}
proc ::music::youtube_close {} {
    variable yt_startup; variable yt_ready
    if {$yt_startup && !$yt_ready} {youtube_collapse} elseif {[exists .youtube]} {destroy .youtube}
}
proc ::music::youtube_options {} {
    variable yt_busy
    if {[exists .youtube_options]} {raise .youtube_options; return}
    card .youtube_options {YouTube options}; wm resizable .youtube_options 0 0
    ttk::frame .youtube_options.body -padding 20; pack .youtube_options.body -fill both -expand 1
    set f .youtube_options.body
    ttk::label $f.heading -text {Sync likes from}; pack $f.heading -anchor w; drag_handle $f.heading .youtube_options
    ttk::combobox $f.source -textvariable ::music::source -values {{YouTube Music liked songs} {YouTube liked videos}} -state readonly -width 32
    pack $f.source -fill x -pady 8
    if {$yt_busy} {$f.source configure -state disabled}
    bind $f.source <<ComboboxSelected>> ::music::options_save
    ttk::checkbutton $f.hosting -text {Phone controls / radio hosting} -variable ::music::hosting -command ::music::options_save
    pack $f.hosting -anchor w -pady 6
    ttk::checkbutton $f.gaming -text {Reduce effects while GTA is running} -variable ::music::auto_gaming -command ::music::options_save
    pack $f.gaming -anchor w -pady 6
    ttk::button $f.playlists -text {Sync Playlists.txt} -command {::music::emit sync_playlists}; pack $f.playlists -fill x -pady 8
    ttk::button $f.forget -text {Forget YouTube login} -command {::music::emit youtube_forget; destroy .youtube_options}; pack $f.forget -fill x -pady 4
    ttk::button $f.done -text Done -command {destroy .youtube_options}; pack $f.done -anchor e -pady {10 0}
    bind .youtube_options <Escape> {destroy .youtube_options; break}
    reveal .youtube_options
    emit options_request
}
proc ::music::options_save {} {variable source; variable hosting; variable auto_gaming; emit options_save $source $hosting $auto_gaming}
proc ::music::options_state {new_source new_hosting new_auto} {variable source; variable hosting; variable auto_gaming; set source $new_source; set hosting $new_hosting; set auto_gaming $new_auto}

# Exact preset arrays are generated from the source class constant, not user settings.
source -encoding utf-8 [file join [file dirname [info script]] presets.tcl]
::music::init
