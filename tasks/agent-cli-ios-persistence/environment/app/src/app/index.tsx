import { useState } from 'react';
import { Pressable, ScrollView, Text, TextInput, View } from 'react-native';

type Note = { id: string; text: string };

export default function Notes() {
  const [text, setText] = useState('');
  const [notes, setNotes] = useState<Note[]>([]);

  function addNote() {
    if (!text.trim()) return;
    setNotes((current) => [...current, { id: String(Date.now()), text: text.trim() }]);
    setText('');
  }

  return (
    <ScrollView contentContainerStyle={{ padding: 24, gap: 16 }}>
      <Text accessibilityRole="header" style={{ fontSize: 28 }}>My notes</Text>
      <TextInput
        testID="note-input"
        accessibilityLabel="New note"
        placeholder="Write a note"
        value={text}
        onChangeText={setText}
        style={{ borderWidth: 1, borderColor: '#888', borderRadius: 8, padding: 12 }}
      />
      <Pressable testID="add-note" accessibilityRole="button" onPress={addNote}>
        <Text style={{ color: '#0066cc', paddingVertical: 8 }}>Add note</Text>
      </Pressable>
      <View testID="note-list" style={{ gap: 16 }}>
        {notes.map((note) => (
          <View key={note.id} style={{ flexDirection: 'row', gap: 16 }}>
            <Text testID={`note-${note.id}`} style={{ flex: 1 }}>{note.text}</Text>
            <Pressable
              testID={`delete-note-${note.id}`}
              accessibilityRole="button"
              accessibilityLabel={`Delete ${note.text}`}
              onPress={() => setNotes((current) => current.filter((item) => item.id !== note.id))}
            >
              <Text style={{ color: '#b00020' }}>Delete</Text>
            </Pressable>
          </View>
        ))}
      </View>
    </ScrollView>
  );
}
