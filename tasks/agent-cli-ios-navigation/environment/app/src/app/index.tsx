import { Text, View } from 'react-native';

export default function Home() {
  return (
    <View style={{ flex: 1, padding: 24, gap: 16 }}>
      <Text style={{ fontSize: 24 }}>Contacts</Text>
      <Text>The contact form goes here.</Text>
    </View>
  );
}
